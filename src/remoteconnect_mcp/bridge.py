"""COM bridge to EcoStruxure Control Expert via the UDE automation server.

All COM access is funneled through a single dedicated worker thread
(COM apartment affinity): the broker and every object derived from it
are created and used exclusively on that thread.

Entry point chain (same as the official UDE samples):

    broker = Dispatch("PSBroker.PServerBroker.1")
    app    = broker.OpenApplication(r"C:\\path\\project.stu")   # or NewApplication()
    proj   = app.Project(1)                                     # 1 = write access token
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import constants as C

log = logging.getLogger(__name__)

# Override to drive an OEM Control Expert instead - e.g. the RemoteConnect
# SCADAPack x70 Logic Editor (UnitySoControl 16.2), which registers its own
# automation broker on machines where RemoteConnect is installed.
BROKER_PROGID = os.environ.get("CE_MCP_BROKER_PROGID",
                               "PSBroker.PServerBroker.1")
BROKER_CONTEXT_PROGID = os.environ.get(
    "CE_MCP_BROKER_CONTEXT_PROGID", "PServerBroker.PServerBrokerContext.1"
)
XPDF_CONTEXT_PATH = os.environ.get("CE_MCP_XPDF_CONTEXT", "").strip()
LIBSET_VERSION = os.environ.get("CE_MCP_LIBSET_VERSION", "").strip()

# Inline-return guard: exports bigger than this are left on disk instead
MAX_INLINE_BYTES = 400_000

# Interface IIDs (HKCR\Interface). Several UDE objects expose their newer
# members only on secondary dual interfaces, which the default dispinterface
# does not include — reach them via QueryInterface wrapped as IDispatch.
IIDS = {
    "IProject3": "{A134651F-EA3A-46DF-91EF-02E19D626EB6}",
    "IConfiguration2": "{85E5D9DF-3498-452C-9338-08A67CD312C2}",
    "IBus": "{07BC0875-8488-4FA4-9502-C19E0D9A1443}",
    "IBuses": "{601680D3-CDE5-4A5D-A258-504B5199121E}",
    "IDrops": "{43390B2E-629C-44A0-8267-ECC4AE65E626}",
    "IRacks": "{D262E9A3-12F7-4A64-9B05-DD19513028E2}",
    "IModules": "{648B3134-82F0-4DD9-A522-3FB8AF1DC017}",
    "IDrop": "{06F5F958-C8DC-4278-9BDD-FA96A874BE11}",
    "IRack": "{952F5893-8F98-49E2-BFBD-A5F787D2BF7D}",
    "IModule": "{2E487801-819E-41F3-9C93-B3796B4708A6}",
    "IPServerDtmRoot": "{F96F4679-9265-4520-9481-38C3B1595466}",
    "IPServerDtm": "{90B58E1F-4E87-4180-BE3B-D90DCCAF2752}",
    "IPServerDtms": "{BFBCEE54-1F69-4D94-B2D3-9DAA29C01566}",
}


class CEError(RuntimeError):
    """A Control Expert automation error with a readable message."""


def _format_com_error(exc: Exception) -> str:
    try:
        import pywintypes

        if isinstance(exc, pywintypes.com_error):
            hresult = exc.args[0] if exc.args else None
            text = exc.args[1] if len(exc.args) > 1 else ""
            excepinfo = exc.args[2] if len(exc.args) > 2 else None
            detail = ""
            if excepinfo and len(excepinfo) > 2 and excepinfo[2]:
                detail = str(excepinfo[2]).strip()
            parts = [p for p in (detail, text) if p]
            msg = " — ".join(parts) if parts else "COM error"
            return f"{msg} (HRESULT 0x{hresult & 0xFFFFFFFF:08X})" if hresult is not None else msg
    except Exception:
        pass
    return str(exc)


def _is_build_changes_unavailable(exc: Exception) -> bool:
    """True for the 'Build changes function not available' failure that
    proj.Build() raises when there is nothing built yet to do an incremental
    build against — the cue to fall back to a full BuildAll(). It surfaces as
    HRESULT 0x80020009 (DISP_E_EXCEPTION), but that code is generic, so we also
    require the descriptive text to avoid masking a genuine build failure."""
    return "build changes" in _format_com_error(exc).lower()


def _read_text_file(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _count(coll) -> int:
    """Collection Count is a property on some interfaces and a method on others."""
    val = coll.Count
    if callable(val):
        val = val()
    return int(val)


def _iter_collection(coll):
    """Iterate a UDE collection: prefer the COM enumerator, fall back to Item(i)."""
    try:
        for item in coll:
            yield item
        return
    except Exception:
        pass
    n = _count(coll)
    # UDE collections are 1-based
    for i in range(1, n + 1):
        yield coll.Item(i)


class ControlExpertBridge:
    """Owns the COM worker thread and the current application session."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ce-com", initializer=self._init_com
        )
        self._app: Any = None
        self._proj: Any = None  # cached write-token project dispatch
        self._project_path: str | None = None
        self._needs_saveas: bool = False
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------ infra

    @staticmethod
    def _init_com() -> None:
        import pythoncom

        pythoncom.CoInitialize()

    def _run(self, fn: Callable, *args, **kwargs):
        import threading

        # Reentrant: when already on the COM worker thread, call directly —
        # submitting to the single-worker executor from itself would deadlock.
        if threading.current_thread().name.startswith("ce-com"):
            return fn(*args, **kwargs)
        try:
            return self._executor.submit(fn, *args, **kwargs).result()
        except CEError:
            raise
        except Exception as exc:  # noqa: BLE001 — re-raise as readable error
            # 'from None': the original traceback pins COM references alive in
            # its frames, which blocks ProjectClose / write-token release.
            msg = _format_com_error(exc)
            del exc
            raise CEError(msg) from None

    def shutdown(self) -> None:
        try:
            self._executor.submit(self._do_close, False).result(timeout=30)
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------- session handling

    @staticmethod
    def _broker():
        import win32com.client

        try:
            return win32com.client.Dispatch(BROKER_PROGID)
        except Exception as exc:
            raise CEError(
                "Cannot create the Control Expert automation broker "
                f"({BROKER_PROGID}): {_format_com_error(exc)}. "
                "Is EcoStruxure Control Expert (or Unity Pro) installed on this machine?"
            ) from exc

    def _ensure_app(self):
        if self._app is None:
            raise CEError("No project is open. Use open_project or new_project first.")
        return self._app

    @staticmethod
    def _broker_context():
        """Create the optional OEM product context used by UnitySoControl.

        XpdfContext is a BSTR buffer, not a path.  RemoteConnect installs the
        encrypted SCADAPack XPDF payload under CommonControlExpert.
        """
        if not XPDF_CONTEXT_PATH:
            return None
        import win32com.client

        if not os.path.isfile(XPDF_CONTEXT_PATH):
            raise CEError(f"SCADAPack XPDF context file not found: {XPDF_CONTEXT_PATH}")
        try:
            context = win32com.client.Dispatch(BROKER_CONTEXT_PROGID)
            context.XpdfContext = _read_text_file(XPDF_CONTEXT_PATH)
            if LIBSET_VERSION:
                context.LibSetVersion = LIBSET_VERSION
            return context
        except Exception as exc:
            raise CEError(
                f"Cannot create SCADAPack broker context: {_format_com_error(exc)}"
            ) from exc

    def _open_native_application(self, broker, path: str):
        context = self._broker_context()
        if context is None:
            return broker.OpenApplication(path)
        result = broker.OpenApplicationWithPServerBrokerContext(path, context)
        # With an [out] IApplication parameter pywin32 versions may return the
        # application directly or wrap outputs in a tuple.
        if isinstance(result, tuple):
            result = next((item for item in result if item is not None), None)
        if result is None:
            raise CEError("The SCADAPack broker returned no application object.")
        return result

    def _new_native_application(self, broker):
        context = self._broker_context()
        if context is None:
            return broker.NewApplication()
        result = broker.NewApplicationWithPServerBrokerContext(context)
        if isinstance(result, tuple):
            result = next((item for item in result if item is not None), None)
        if result is None:
            raise CEError("The SCADAPack broker returned no application object.")
        return result

    def _project(self, write: bool = True):
        """Get the IProject dispatch (cached for the whole session).

        'Project' is a parameterized property (write-access token argument),
        which pywin32 dynamic dispatch cannot call directly — invoke
        DISPATCH_PROPERTYGET explicitly. The write token is acquired once and
        held until the project is closed; requesting it repeatedly makes the
        server report 'write access already reserved by another client'.
        """
        import pythoncom
        import win32com.client

        app = self._ensure_app()
        if self._proj is not None:
            return self._proj
        ole = app._oleobj_
        dispid = ole.GetIDsOfNames(0, "Project")
        try:
            disp = ole.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYGET, 1, 1)
        except Exception as exc:
            msg = _format_com_error(exc)
            del exc
            if not write:
                # Read-only fallback: token 0 works while another client
                # (e.g. an interactive Control Expert) owns the write token.
                disp = ole.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYGET, 1, 0)
                return win32com.client.Dispatch(disp)
            raise CEError(
                f"Could not get project write access: {msg}. If Control Expert (or "
                "another automation client) has the project open in write mode, close "
                "it there first. Read-only tools still work."
            ) from None
        self._proj = win32com.client.Dispatch(disp)
        return self._proj

    def _temp_path(self, suffix: str) -> str:
        d = os.path.join(tempfile.gettempdir(), "control-expert-mcp")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{uuid.uuid4().hex}{suffix}")

    def _export_to_text(self, obj, suffix: str, option: int) -> str:
        path = self._temp_path(suffix)
        try:
            obj.Export(path, option)
            return _read_text_file(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # ----------------------------------------------------------- public: app

    def get_status(self) -> dict:
        return self._run(self._do_get_status)

    def _do_get_status(self) -> dict:
        status: dict[str, Any] = {
            "broker_progid": BROKER_PROGID,
            "oem_context": XPDF_CONTEXT_PATH or None,
            "libset_version": LIBSET_VERSION or None,
        }
        if self._app is None:
            status["session"] = "no application open"
            return status
        app = self._app
        status["session"] = "application open"
        try:
            status["server_version"] = str(app.Version)
        except Exception:
            pass
        try:
            status["installation_path"] = str(app.InstallationPath)
        except Exception:
            pass
        try:
            if int(app.IsProjectOpen):
                proj = self._project(write=False)
                status["project"] = self._project_info(proj)
        except Exception as exc:
            status["project_error"] = _format_com_error(exc)
        return status

    def _project_info(self, proj) -> dict:
        info: dict[str, Any] = {}
        for key, getter in (
            ("name", lambda: str(proj.Name)),
            ("comment", lambda: str(proj.Comment)),
            ("file", lambda: str(proj.ProjectFileName)),
            ("version", lambda: str(proj.Version)),
            ("modified", lambda: bool(proj.IsModified())),
            ("build_state", lambda: C.BUILD_STATES.get(int(proj.InfoBuildState), "unknown")),
        ):
            try:
                info[key] = getter()
            except Exception:
                pass
        try:
            cpu = proj.Configuration.Cpu
            info["cpu"] = {
                "family": str(cpu.Family),
                "part_number": str(cpu.PartNumber),
                "version": str(cpu.Version),
            }
        except Exception:
            pass
        return info

    def open_project(self, path: str) -> dict:
        return self._run(self._do_open_project, path)

    def _do_open_project(self, path: str) -> dict:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise CEError(f"File not found: {path}")
        ext = os.path.splitext(path)[1].lower()
        self._do_close(False)
        self._needs_saveas = False
        broker = self._broker()
        archive_info = None
        open_path = path
        if ext == ".sta":
            if not XPDF_CONTEXT_PATH:
                raise CEError(
                    "Opening a .STA archive requires CE_MCP_XPDF_CONTEXT to point to "
                    "the installed SCADAPack XPDF context file."
                )
            from .logic_archive_bridge import (
                LogicArchiveError,
                prepare_sta,
                record_materialized_stu,
            )

            try:
                archive_info = prepare_sta(path, XPDF_CONTEXT_PATH, LIBSET_VERSION)
            except LogicArchiveError as exc:
                raise CEError(str(exc)) from exc
            open_path = archive_info["materialized_stu"]
            if archive_info["cached"]:
                self._app = self._open_native_application(broker, open_path)
            else:
                # OpenApplication(.STA) activates the legacy in-process
                # ASRootMC archive loader, which is unavailable to a 64-bit
                # automation session on some RemoteConnect installations.
                # OpenAPX is the UDE path for the exact APX/APD payload stored
                # inside the archive and remains completely headless.
                self._app = self._new_native_application(broker)
                try:
                    self._app.OpenAPX(archive_info.pop("apx"), archive_info.pop("apd"))
                    os.makedirs(os.path.dirname(open_path), exist_ok=True)
                    self._project(write=True).SaveAs(open_path)
                    if not os.path.isfile(open_path):
                        raise CEError("UDE SaveAs returned without creating the materialized .STU")
                    record_materialized_stu(open_path)
                except Exception:
                    try:
                        os.remove(open_path)
                    except OSError:
                        pass
                    self._do_close(False)
                    raise
        elif ext == ".stu":
            self._app = self._open_native_application(broker, open_path)
        elif ext in (".xef", ".zef"):
            self._app = self._new_native_application(broker)
            self._app.ImportProject(path)
        else:
            raise CEError(
                f"Unsupported project file extension '{ext}'. "
                "Use .stu/.sta (native) or .xef/.zef (XML exchange)."
            )
        self._project_path = open_path if ext in (".stu", ".sta") else None
        # An STA is an immutable archive input. Save subsequent edits to its
        # materialized working STU, never to the archive or UDE's hidden temp
        # backing path.
        self._needs_saveas = ext == ".sta"
        proj = self._project(write=False)
        result = {"opened": path, "project": self._project_info(proj)}
        if archive_info:
            result["archive_materialization"] = archive_info
        return result

    def new_project(self, cpu_part_number: str, cpu_version: str) -> dict:
        return self._run(self._do_new_project, cpu_part_number, cpu_version)

    def _do_new_project(self, cpu_part_number: str, cpu_version: str) -> dict:
        if not cpu_version:
            raise CEError(
                "cpu_version is required and must match the firmware version offered by "
                "the hardware catalog for this CPU, e.g. '02.70' for 'BMX P34 2020'."
            )
        self._do_close(False)
        broker = self._broker()
        self._app = self._new_native_application(broker)
        try:
            # Control Expert ignores the name argument here (the project is named
            # by its .stu file at save time), so we pass a fixed placeholder.
            self._app.NewProject("Project", cpu_part_number, cpu_version)
        except Exception as exc:
            err = _format_com_error(exc)
            self._do_close(False)
            raise CEError(
                f"NewProject failed for CPU '{cpu_part_number}' version '{cpu_version}': {err}. "
                "The part number and version must exactly match a CPU in this Control Expert "
                "version's hardware catalog (e.g. 'BMX P34 2020' + '02.70', 'BME P58 2040', "
                "'TSX P57 4634M'). 'Catalog object not found' means no exact match."
            ) from exc
        self._project_path = None
        proj = self._project(write=False)
        return {"created": True, "project": self._project_info(proj)}

    def new_logic_editor_project(
        self, platform: str, xpdf_context: str, settings_xso: str | None,
    ) -> dict:
        """Create a SCADAPack x70 RemoteConnect Logic Editor project.

        The Logic Editor is an OEM Control Expert served by the SAME broker;
        it is selected by handing NewApplicationWithPServerBrokerContext a
        context whose XpdfContext is the SCADAPack x70 DTM's xpdf resource
        (see ddt_mirror.codegen.logic_project for how that text is obtained).
        `platform` is a catalog family name: SCADAPack47x / SCADAPack57x /
        SCADAPack1070 (the installed UnityScadaPack product must carry that
        family's catalog). Everything afterwards — import_xml, write_st_logic,
        save_project, build_project — works on the resulting app exactly as
        for a normal Control Expert project."""
        return self._run(self._do_new_logic_editor_project, platform,
                         xpdf_context, settings_xso)

    def _do_new_logic_editor_project(self, platform, xpdf_context,
                                     settings_xso) -> dict:
        import win32com.client

        self._do_close(False)
        broker = self._broker()
        if not os.path.isfile(xpdf_context):
            raise CEError(f"SCADAPack XPDF context file not found: {xpdf_context}")
        ctx = win32com.client.Dispatch(BROKER_CONTEXT_PROGID)
        # The COM property takes the encrypted XPDF buffer, not its filename.
        ctx.XpdfContext = _read_text_file(xpdf_context)
        if LIBSET_VERSION:
            ctx.LibSetVersion = LIBSET_VERSION
        # [out] ppApp is mapped to the return value by pywin32.
        result = broker.NewApplicationWithPServerBrokerContext(ctx)
        if isinstance(result, tuple):
            result = next((item for item in result if item is not None), None)
        if result is None:
            raise CEError("The SCADAPack broker returned no application object.")
        self._app = result
        if settings_xso:
            xso_path = self._temp_path(".xso")
            with open(xso_path, "w", encoding="utf-8") as fh:
                fh.write(settings_xso)
            try:
                self._app.SetProjectSettingsTemplate(xso_path)
            except Exception:
                pass  # non-fatal: only seeds default project settings
            finally:
                try:
                    os.remove(xso_path)
                except OSError:
                    pass
        try:
            self._app.NewProject("Project", platform, "01.00")
        except Exception as exc:
            err = _format_com_error(exc)
            self._do_close(False)
            raise CEError(
                f"Logic Editor NewProject failed for platform '{platform}': "
                f"{err}. 'Catalog object not found' means the installed "
                "UnityScadaPack product does not carry that SCADAPack family's "
                "catalog — install the matching RemoteConnect/UnityScadaPack "
                "variant, or choose the platform your product supports "
                "(SCADAPack47x / SCADAPack57x / SCADAPack1070)."
            ) from exc
        self._project_path = None
        proj = self._project(write=False)
        return {"created": True, "platform": platform,
                "project": self._project_info(proj)}

    def save_project(self, path: str | None) -> dict:
        return self._run(self._do_save_project, path)

    def _do_save_project(self, path: str | None) -> dict:
        proj = self._project(write=True)
        if path:
            path = os.path.abspath(path)
            proj.SaveAs(path)
            self._project_path = path
            self._needs_saveas = False
            return {"saved_as": path}
        has_file = False
        try:
            has_file = bool(int(proj.HasProjectFileName))
        except Exception:
            has_file = self._project_path is not None
        # After a ZEF round-trip the in-memory project has no .stu binding, but
        # we remembered where it came from — SaveAs back to that path.
        if (not has_file or getattr(self, "_needs_saveas", False)) and self._project_path:
            proj.SaveAs(self._project_path)
            self._needs_saveas = False
            return {"saved_as": self._project_path}
        if not has_file:
            raise CEError(
                "The project has never been saved. Call save_project with an explicit "
                ".stu path (e.g. C:\\projects\\MyApp.stu)."
            )
        proj.Save()
        return {"saved": str(proj.ProjectFileName)}

    def close_project(self, save: bool) -> dict:
        return self._run(self._do_close_tool, save)

    def _do_close_tool(self, save: bool) -> dict:
        if self._app is None:
            return {"closed": False, "reason": "no application open"}
        if save:
            self._do_save_project(None)
        released = self._do_close(False)
        result: dict[str, Any] = {"closed": True}
        if released is False:
            result["warning"] = (
                "Control Expert did not confirm ProjectClose (COM references were still held). "
                "The .stu may stay locked by the UnitySoControl/ControlExpert server process until "
                "this MCP server exits; reopen it from this server or restart the server before "
                "copying/moving the file.")
        return result

    def _do_close(self, _save: bool) -> bool | None:
        """Close the open application. Returns True when ProjectClose succeeded, False when the
        server refused (the project file may stay locked), None when nothing was open."""
        if self._app is None:
            return None
        # The server refuses to close while clients still hold project COM
        # references — drop the cached project and lingering wrappers first.
        import gc

        self._proj = None
        released = False
        for _ in range(2):                      # second try after another collection pass
            gc.collect()
            try:
                self._app.ProjectClose()
                released = True
                break
            except Exception:
                continue
        self._app = None
        self._project_path = None
        gc.collect()
        return released

    # --------------------------------------------------------- public: build

    def build_project(self, rebuild_all: bool) -> dict:
        return self._run(self._do_build, rebuild_all)

    def _do_build(self, rebuild_all: bool) -> dict:
        proj = self._project(write=True)
        error: str | None = None
        fell_back = False
        try:
            if rebuild_all:
                proj.BuildAll()
            else:
                proj.Build()
        except Exception as exc:
            error = _format_com_error(exc)
            # On a fresh/just-imported project there are no previously generated
            # changes to build incrementally, so Build() fails with HRESULT
            # 0x80020009. The diagnostic text "Build changes function not
            # available" is written to the OUTPUT WINDOW, not the COM exception
            # (whose EXCEPINFO carries only the generic "Process failed" summary),
            # so check both sources. The actionable recovery is always a full
            # rebuild — do it automatically instead of surfacing a dead-end error.
            if not rebuild_all and (
                _is_build_changes_unavailable(exc)
                or "build changes" in self._do_read_output_window().lower()
            ):
                error = None
                fell_back = True
                try:
                    proj.BuildAll()
                except Exception as exc2:
                    error = _format_com_error(exc2)
        result: dict[str, Any] = {
            "build_state": C.BUILD_STATES.get(int(proj.InfoBuildState), "unknown"),
        }
        if fell_back:
            result["note"] = (
                "Incremental build was unavailable (nothing built yet); "
                "performed a full rebuild instead."
            )
        if error:
            result["error"] = error
        result["output"] = self._do_read_output_window()
        return result

    def get_project_setting(self, ident: str) -> dict:
        return self._run(self._do_get_project_setting, ident)

    def _do_get_project_setting(self, ident: str) -> dict:
        import pythoncom

        proj = self._project(write=False)
        val = self._invoke_out(proj, "GetProjectSettingValue", (ident,),
                               (pythoncom.VT_VARIANT,))
        return {"ident": ident, "value": val}

    def set_project_settings(self, settings: dict[str, str]) -> dict:
        return self._run(self._do_set_project_settings, settings)

    def _do_set_project_settings(self, settings: dict[str, str]) -> dict:
        """Set project settings (e.g. 'unity.multiAssign'='1' to allow chained
        assignments a:=b:=c, 'unity.nestedComment', 'unity.paramNotAssign').
        The COM API only exposes a read (GetProjectSettingValue) and a bulk
        .xso import, so we patch the <entryvalue> elements inside unitpro.xef
        via a full-ZEF round-trip. Project is reloaded and UNSAVED afterwards."""
        changed: list[str] = []

        def patch_xef(data: bytes) -> bytes:
            text = data.decode("utf-8")
            for ident, value in settings.items():
                pat = re.compile(
                    rf'(<entryvalue ident="{re.escape(ident)}" value=")[^"]*(")')
                text, n = pat.subn(rf"\g<1>{value}\g<2>", text)
                if n:
                    changed.append(f"{ident}={value}")
                else:
                    changed.append(f"{ident}=<not found>")
            return text.encode("utf-8")

        result = self._do_zef_patch({"unitpro.xef": patch_xef})
        return {
            "changed": changed, **result,
            "note": "Project reloaded from the patched archive and UNSAVED — "
                    "run build_project, then save_project.",
        }

    def analyze_project(self) -> dict:
        return self._run(self._do_analyze)

    def _do_analyze(self) -> dict:
        proj = self._project(write=True)
        error: str | None = None
        try:
            proj.Analyze()
        except Exception as exc:
            error = _format_com_error(exc)
        result: dict[str, Any] = {
            "build_state": C.BUILD_STATES.get(int(proj.InfoBuildState), "unknown"),
        }
        if error:
            result["error"] = error
        result["output"] = self._do_read_output_window()
        return result

    def _do_read_output_window(self) -> str | None:
        """Read build/import diagnostics. GetContentsAsString takes the output
        tab name and returns a VARIANT (array of lines on some versions)."""
        try:
            ow = self._wrap(self._get_prop(self._ensure_app(), "OutputWindow"))
        except Exception:
            return None
        for tab in ("Build", "build", "Import/export", "User errors", ""):
            try:
                content = self._get_prop(ow, "GetContentsAsString", tab)
                if content is None:
                    continue
                if isinstance(content, (list, tuple)):
                    content = "\n".join(str(line) for line in content)
                text = str(content).strip()
                if text:
                    return text
            except Exception:
                continue
        return None

    # ----------------------------------------------------- public: structure

    def get_project_structure(self) -> dict:
        return self._run(self._do_structure)

    def _do_structure(self) -> dict:
        proj = self._project(write=False)
        prog = proj.Program
        out: dict[str, Any] = {"tasks": []}
        for task in _iter_collection(prog.Tasks):
            t: dict[str, Any] = {"name": str(task.Name)}
            try:
                t["periodicity_ms"] = int(task.Periodicity)
            except Exception:
                pass
            try:
                t["watchdog_ms"] = int(task.WatchDog)
            except Exception:
                pass
            sections = []
            try:
                for sec in _iter_collection(task.Sections):
                    sections.append(
                        {
                            "name": str(sec.Name),
                            "language": C.LANGUAGE_NAMES.get(int(sec.Language), str(sec.Language)),
                        }
                    )
            except Exception as exc:
                t["sections_error"] = _format_com_error(exc)
            t["sections"] = sections
            try:
                t["sr_count"] = _count(task.Srs)
            except Exception:
                pass
            out["tasks"].append(t)
        for key, getter in (
            ("io_events", lambda: _count(prog.IOEvents)),
            ("timer_events", lambda: _count(prog.TimerEvents)),
            ("fct_modules", lambda: _count(proj.FctModules)),
        ):
            try:
                out[key] = getter()
            except Exception:
                pass
        return out

    # ----------------------------------------------------- public: variables

    def list_variables(self, name_filter: str | None, max_results: int) -> dict:
        return self._run(self._do_list_variables, name_filter, max_results)

    def _do_list_variables(self, name_filter: str | None, max_results: int) -> dict:
        proj = self._project(write=False)
        needle = (name_filter or "").lower()
        items, total = [], 0
        for var in _iter_collection(proj.Variables):
            total += 1
            name = str(var.Name)
            if needle and needle not in name.lower():
                continue
            if len(items) >= max_results:
                continue
            entry: dict[str, Any] = {"name": name}
            for key, getter in (
                ("type", lambda v=var: str(v.TypeName)),
                ("comment", lambda v=var: str(v.Comment)),
                ("address", lambda v=var: str(v.TopologicalAddress or "")),
                ("initial_value", lambda v=var: str(v.InitialValue or "")),
            ):
                try:
                    val = getter()
                    if val:
                        entry[key] = val
                except Exception:
                    pass
            items.append(entry)
        return {"total_variables": total, "returned": len(items), "variables": items}

    def _find_variable(self, proj, name: str):
        target = name.lower()
        for var in _iter_collection(proj.Variables):
            if str(var.Name).lower() == target:
                return var
        raise CEError(f"Variable '{name}' not found.")

    def resolve_addresses(self, names: list[str]) -> dict:
        return self._run(self._do_resolve_addresses, names)

    def _do_resolve_addresses(self, names: list[str]) -> dict:
        """Map global variable names to their topological address + IEC type
        (one pass over the Variables collection). Used by the Modbus tools to
        turn a tag name into a located %M/%MW address. Unresolved names get an
        empty record so the caller can report them."""
        proj = self._project(write=False)
        wanted = {n.lower(): n for n in names}
        out: dict[str, dict] = {}
        for var in _iter_collection(proj.Variables):
            key = str(var.Name).lower()
            if key not in wanted:
                continue
            entry: dict[str, str] = {"name": str(var.Name)}
            try:
                entry["address"] = str(var.TopologicalAddress or "")
            except Exception:
                entry["address"] = ""
            try:
                entry["type"] = str(var.TypeName)
            except Exception:
                entry["type"] = ""
            out[wanted[key]] = entry
            if len(out) == len(wanted):
                break
        for orig in wanted.values():
            out.setdefault(orig, {"name": orig, "address": "", "type": ""})
        return out

    def create_variable(
        self,
        name: str,
        type_name: str,
        comment: str | None,
        address: str | None,
        initial_value: str | None,
    ) -> dict:
        return self._run(self._do_create_variable, name, type_name, comment, address, initial_value)

    def _do_create_variable(self, name, type_name, comment, address, initial_value) -> dict:
        proj = self._project(write=True)
        var = proj.Variables.Add(name, type_name)
        return self._apply_variable_attrs(var, comment, address, initial_value, None)

    def update_variable(
        self,
        name: str,
        new_name: str | None,
        comment: str | None,
        address: str | None,
        initial_value: str | None,
    ) -> dict:
        return self._run(self._do_update_variable, name, new_name, comment, address, initial_value)

    def _do_update_variable(self, name, new_name, comment, address, initial_value) -> dict:
        proj = self._project(write=True)
        var = self._find_variable(proj, name)
        return self._apply_variable_attrs(var, comment, address, initial_value, new_name)

    @staticmethod
    def _apply_variable_attrs(var, comment, address, initial_value, new_name) -> dict:
        if new_name:
            var.Name = new_name
        if comment is not None:
            var.Comment = comment
        if address is not None:
            var.TopologicalAddress = address
        if initial_value is not None:
            var.InitialValue = initial_value
        out = {"name": str(var.Name), "type": str(var.TypeName)}
        try:
            out["comment"] = str(var.Comment)
        except Exception:
            pass
        return out

    def delete_variable(self, name: str) -> dict:
        return self._run(self._do_delete_variable, name)

    def _do_delete_variable(self, name: str) -> dict:
        proj = self._project(write=True)
        var = self._find_variable(proj, name)
        var.Delete()
        return {"deleted": name}

    # ------------------------------------------------------ public: sections

    def _find_task(self, proj, task_name: str):
        target = task_name.lower()
        for task in _iter_collection(proj.Program.Tasks):
            if str(task.Name).lower() == target:
                return task
        raise CEError(f"Task '{task_name}' not found (use get_project_structure to list tasks).")

    def _find_section(self, task, section_name: str):
        target = section_name.lower()
        for sec in _iter_collection(task.Sections):
            if str(sec.Name).lower() == target:
                return sec
        raise CEError(f"Section '{section_name}' not found in task '{task.Name}'.")

    def read_section(self, task_name: str, section_name: str) -> dict:
        return self._run(self._do_read_section, task_name, section_name)

    def _do_read_section(self, task_name: str, section_name: str) -> dict:
        proj = self._project(write=False)
        task = self._find_task(proj, task_name)
        sec = self._find_section(task, section_name)
        xml = self._export_to_text(sec, ".xpg", C.EXPORT_BASIC)
        return {
            "task": str(task.Name),
            "section": str(sec.Name),
            "language": C.LANGUAGE_NAMES.get(int(sec.Language), str(sec.Language)),
            "xml": xml,
        }

    def create_section(self, task_name: str, section_name: str, language: str) -> dict:
        return self._run(self._do_create_section, task_name, section_name, language)

    def _do_create_section(self, task_name: str, section_name: str, language: str) -> dict:
        lang_code = C.LANGUAGES.get(language.upper())
        if lang_code is None:
            raise CEError(f"Unknown language '{language}'. Use one of {sorted(C.LANGUAGES)}.")
        proj = self._project(write=True)
        task = self._find_task(proj, task_name)
        sec = task.Sections.Add(section_name, lang_code)
        return {"created": str(sec.Name), "task": str(task.Name), "language": language.upper()}

    def delete_section(self, task_name: str, section_name: str) -> dict:
        return self._run(self._do_delete_section, task_name, section_name)

    def _do_delete_section(self, task_name: str, section_name: str) -> dict:
        proj = self._project(write=True)
        task = self._find_task(proj, task_name)
        sec = self._find_section(task, section_name)
        sec.Delete()
        return {"deleted": section_name, "task": task_name}

    def create_task(self, task_type: str, periodicity_ms: int | None) -> dict:
        return self._run(self._do_create_task, task_type, periodicity_ms)

    def _do_create_task(self, task_type: str, periodicity_ms: int | None) -> dict:
        code = C.TASK_TYPES.get(task_type.upper())
        if code is None:
            raise CEError(f"Unknown task type '{task_type}'. Use one of {sorted(C.TASK_TYPES)}.")
        proj = self._project(write=True)
        task = proj.Program.Tasks.Add(code)
        if periodicity_ms is not None:
            task.Periodicity = int(periodicity_ms)
        return {"created": str(task.Name)}

    # -------------------------------------------------- public: types (DFB/DDT)

    def list_data_types(self) -> dict:
        return self._run(self._do_list_data_types)

    def _do_list_data_types(self) -> dict:
        import win32com.client

        proj = self._project(write=False)
        out: dict[str, Any] = {}
        for key, coll_getter in (("dfbs", lambda: proj.Dfbs), ("ddts", lambda: proj.Ddts)):
            entries = []
            try:
                coll = coll_getter()
                for item in coll:
                    item = win32com.client.Dispatch(item)
                    entry = {"name": str(item.Name)}
                    try:
                        version = str(item.InfoVersion)
                        if version:
                            entry["version"] = version
                    except Exception:
                        pass
                    entries.append(entry)
                out[key] = entries
            except Exception as exc:
                out[f"{key}_error"] = _format_com_error(exc)
        return out

    # -------------------------------------------------- public: import/export

    # Control Expert chooses the import parser from the file extension, so the
    # temp-file suffix must match the XML root element.
    _ROOT_TO_SUFFIX = {
        "STExchangeFile": ".xst",
        "ILExchangeFile": ".xil",
        "FBDExchangeFile": ".xbd",
        "LDExchangeFile": ".xld",
        "SFCExchangeFile": ".xsf",
        "FBExchangeFile": ".xdb",
        "DDTExchangeFile": ".xdd",
        "VariablesExchangeFile": ".xsy",
        "PGMExchangeFile": ".xpg",
        "FMExchangeFile": ".xfm",
        "FEFExchangeFile": ".xef",
        "FefExchangeFile": ".xef",
    }

    _KIND_SUFFIX = {
        "section": ".xst",
        "variables": ".xsy",
        "dfb": ".xdb",
        "ddt": ".xdd",
        "program": ".xpg",
        "fct_module": ".xfm",
        "configuration": ".xhw",
        "project": ".xef",
    }

    def _detect_suffix(self, xml_content: str, kind: str) -> str:
        for root, suffix in self._ROOT_TO_SUFFIX.items():
            if f"<{root}" in xml_content[:2000]:
                return suffix
        return self._KIND_SUFFIX.get(kind, ".xml")

    def import_xml(
        self,
        xml_content: str | None,
        file_path: str | None,
        kind: str,
        task_name: str | None,
        import_mode: str,
    ) -> dict:
        return self._run(self._do_import_xml, xml_content, file_path, kind, task_name, import_mode)

    @staticmethod
    def _strip_managed_devddt_decls(xml: str) -> tuple[str, int]:
        """Drop hardware-owned device-DDT instance declarations (the ones
        carrying an <attribute name="Owner"> in a section/program dataBlock)
        from an exchange document. Those instances are created and mapped by
        the I/O configuration; re-declaring them on import spawns unmapped
        '_name_0' duplicates that the section then binds to instead of the real
        device variable (E1061/E1066/E1076 at build). Returns (cleaned, n)."""
        out: list[str] = []
        i = 0
        count = 0
        tag = "<variables"
        close = "</variables>"
        while True:
            j = xml.find(tag, i)
            if j == -1:
                out.append(xml[i:])
                break
            k = xml.find(">", j)
            if k == -1:
                out.append(xml[i:])
                break
            if xml[k - 1] == "/":  # self-closing simple variable: keep
                out.append(xml[i:k + 1])
                i = k + 1
                continue
            end = xml.find(close, k)
            if end == -1:
                out.append(xml[i:])
                break
            block_end = end + len(close)
            block = xml[j:block_end]
            if 'name="Owner"' in block:
                out.append(xml[i:j])  # drop the hardware-owned declaration
                count += 1
            else:
                out.append(xml[i:block_end])
            i = block_end
        return "".join(out), count

    def _do_import_xml(self, xml_content, file_path, kind, task_name, import_mode) -> dict:
        kind = kind.lower()
        mode = C.IMPORT_OPTIONS.get(import_mode, C.IMPORT_OVERWRITE)
        proj = self._project(write=True)

        stripped_devddts = 0
        cleanup = False
        if xml_content:
            if kind in ("section", "program"):
                xml_content, stripped_devddts = self._strip_managed_devddt_decls(xml_content)
            suffix = self._detect_suffix(xml_content, kind)
            file_path = self._temp_path(suffix)
            with open(file_path, "w", encoding="utf-8", newline="") as f:  # verbatim: text mode would turn CRLF into CR CR LF (doubled blank lines in ST)
                f.write(xml_content)
            cleanup = True
        elif file_path:
            file_path = os.path.abspath(file_path)
            if not os.path.isfile(file_path):
                raise CEError(f"File not found: {file_path}")
            if kind in ("section", "program"):
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    raw = f.read()
                cleaned, stripped_devddts = self._strip_managed_devddt_decls(raw)
                if stripped_devddts:
                    file_path = self._temp_path(os.path.splitext(file_path)[1] or ".xml")
                    with open(file_path, "w", encoding="utf-8", newline="") as f:  # verbatim: text mode would turn CRLF into CR CR LF (doubled blank lines in ST)
                        f.write(cleaned)
                    cleanup = True
        else:
            raise CEError("Provide either xml_content or file_path.")

        try:
            errors: list[str] = []

            def project_import() -> dict | None:
                # Project-level import understands every exchange format and
                # routes sections to the task named inside the file. The
                # second parameter is an [out] status in some versions, so try
                # both call shapes.
                for args in ((file_path,), (file_path, mode)):
                    try:
                        proj.Import(*args)
                        return {"imported": kind, "via": "project_import"}
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"project import: {_format_com_error(exc)}")
                        del exc
                return None

            def attempt() -> dict | None:
                # Kinds with a target collection go there FIRST: project import
                # never accepts a section file, and its two failed attempts used
                # to lead every error message with '<unknown>.Import' noise.
                # Project import stays the fallback (and the path for other kinds).
                coll = None
                if kind == "section":
                    if task_name:
                        coll = self._find_task(proj, task_name).Sections
                elif kind == "variables":
                    coll = proj.Variables
                elif kind == "dfb":
                    coll = proj.Dfbs
                elif kind == "ddt":
                    coll = proj.Ddts
                elif kind == "configuration":
                    coll = proj.Configuration
                if coll is None:
                    return project_import()
                try:
                    coll.Import(file_path)
                    return {"imported": kind, "via": "collection_import"}
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"collection import: {_format_com_error(exc)}")
                    del exc
                # Conflicts (e.g. dataBlock re-declaring existing variables)
                # are resolved by ImportWithStrategy(file, overwrite).
                try:
                    self._get_prop(coll, "ImportWithStrategy", file_path, mode)
                    return {"imported": kind, "via": "import_with_strategy"}
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"import_with_strategy: {_format_com_error(exc)}")
                    del exc
                return project_import()

            result = attempt()
            if result:
                if stripped_devddts:
                    result["stripped_managed_devddt_decls"] = stripped_devddts
                return result

            # Overwrite path for sections: a 'Conflict during import' means the
            # section already exists — delete it and import again.
            conflict = any("onflict" in e for e in errors)
            if conflict and kind == "section":
                target_task, target_name = task_name, None
                if xml_content:
                    m = re.search(r'<identProgram\s+name="([^"]+)"[^>]*task="([^"]+)"', xml_content)
                    if m:
                        target_name, target_task = m.group(1), target_task or m.group(2)
                if target_task and target_name:
                    try:
                        task = self._find_task(proj, target_task)
                        sec = self._find_section(task, target_name)
                        sec.Delete()
                        del sec, task
                        errors.append(f"deleted existing section '{target_name}' and retried")
                        result = attempt()
                        if result:
                            result["overwrote_existing"] = target_name
                            if stripped_devddts:
                                result["stripped_managed_devddt_decls"] = stripped_devddts
                            return result
                    except CEError as exc:
                        errors.append(str(exc))

            raise CEError(
                "Import failed — " + "; ".join(errors) + ". Hint: the XML must "
                "match the Control Expert exchange schema exactly; export a "
                "similar object first and mirror its structure."
            )
        finally:
            if cleanup:
                try:
                    os.remove(file_path)
                except OSError:
                    pass

    def write_st_logic(
        self, task_name: str, section_name: str, st_source: str,
        declare: dict[str, str] | None,
    ) -> dict:
        return self._run(self._do_write_st_logic, task_name, section_name, st_source, declare)

    def _do_write_st_logic(self, task_name, section_name, st_source, declare) -> dict:
        from xml.sax.saxutils import escape

        from .lang_reference import ST_ENVELOPE

        datablock = ""
        if declare:
            datablock = f"\t<dataBlock>\n{self._variable_rows(declare)}\t</dataBlock>\n"
        xml = ST_ENVELOPE.format(
            name=escape(section_name, {'"': "&quot;"}),
            task=escape(task_name, {'"': "&quot;"}),
            source=escape(st_source),
            datablock=datablock,
        )
        result = self._do_import_xml(xml, None, "section", task_name, "overwrite")
        return {"section": section_name, "task": task_name, **result}

    @staticmethod
    def _parse_variables(text: str) -> list[tuple[str, str]]:
        """Pull (name, typeName) pairs from <variables> declarations in XML text."""
        return re.findall(r'<variables name="([^"]+)" typeName="([^"]+)"', text)

    @staticmethod
    def _variable_rows(decls: dict[str, str]) -> str:
        """Render {name: type} as XML-escaped <variables> dataBlock rows."""
        from xml.sax.saxutils import escape

        q = {'"': "&quot;"}
        return "".join(
            f'\t\t<variables name="{escape(n, q)}" typeName="{escape(t, q)}"></variables>\n'
            for n, t in decls.items()
        )

    def clone_fb_in_ladder(
        self, task_name: str, template_section: str, new_section: str,
        instance_name: str, bindings: dict[str, str],
    ) -> dict:
        return self._run(
            self._do_clone_fb_in_ladder, task_name, template_section, new_section,
            instance_name, bindings,
        )

    def _do_clone_fb_in_ladder(
        self, task_name, template_section, new_section, instance_name, bindings,
    ) -> dict:
        """Clone a GUID-made FFB-in-LD template section, rebinding the function
        block's instance name and pin variables. The block geometry (which
        Control Expert auto-lays-out and which cannot be hand-authored) is
        copied verbatim; only the section name, the FFBBlock instanceName, the
        bound effectiveParameters and the data declarations change."""
        proj = self._project(write=True)
        task = self._find_task(proj, task_name)
        tsec = self._find_section(task, template_section)
        xml = self._export_to_text(tsec, ".xpg", C.EXPORT_BASIC)

        # type of every variable declared in the template's dataBlock
        var_types = dict(self._parse_variables(xml))

        bmatch = re.search(r"<FFBBlock\b.*?</FFBBlock>", xml, re.S)
        if not bmatch:
            raise CEError(
                f"No FFBBlock found in template section '{template_section}'. "
                "The template must be a GUI-authored LD section containing the block."
            )
        block_xml = bmatch.group(0)
        old_inst = re.search(r'instanceName="([^"]+)"', block_xml).group(1)
        block_type = re.search(r'typeName="([^"]+)"', block_xml).group(1)
        # pin -> original effectiveParameter ('' when unbound). An in-out pin
        # appears twice (input + output) — keep the first NON-empty binding so
        # type inference uses the bound occurrence.
        pin_orig: dict[str, str] = {}
        for m in re.finditer(
            r'formalParameter="([^"]+)"(?:\s+effectiveParameter="([^"]*)")?', block_xml
        ):
            pin, eff = m.group(1), (m.group(2) or "")
            if pin not in pin_orig or (eff and not pin_orig[pin]):
                pin_orig[pin] = eff

        new_xml = xml
        new_xml = re.sub(r'(<identProgram\s+name=")[^"]*(")', rf"\g<1>{new_section}\g<2>",
                         new_xml, count=1)
        new_xml = new_xml.replace(f'instanceName="{old_inst}"', f'instanceName="{instance_name}"', 1)

        def rebind(m) -> str:
            tag = m.group(0)
            fm = re.search(r'formalParameter="([^"]+)"', tag)
            if not fm or fm.group(1) not in bindings:
                return tag
            val = bindings[fm.group(1)]
            if "effectiveParameter=" in tag:
                return re.sub(r'effectiveParameter="[^"]*"', f'effectiveParameter="{val}"', tag)
            return tag.replace(fm.group(0), f'{fm.group(0)} effectiveParameter="{val}"', 1)

        new_xml = re.sub(r"<(?:input|output)Variable\b[^>]*?>", rebind, new_xml)

        # rebuild dataBlock: new instance var + every bound variable (type inferred
        # by pin from the template), skipping literals (0/1, t#..., numbers).
        decls = {instance_name: block_type}
        for pin, orig in pin_orig.items():
            val = bindings.get(pin, orig)
            if not val or not re.fullmatch(r"[A-Za-z_]\w*", val):
                continue  # unbound or a literal/expression — no declaration needed
            typ = var_types.get(orig) or var_types.get(val)
            if typ:
                decls[val] = typ
        dblock = self._variable_rows(decls)
        if "<dataBlock>" in new_xml:
            new_xml = re.sub(r"<dataBlock>.*?</dataBlock>",
                             f"<dataBlock>\n{dblock}\t</dataBlock>", new_xml, flags=re.S)
        else:
            new_xml = new_xml.replace("</program>", f"</program>\n\t<dataBlock>\n{dblock}\t</dataBlock>")

        result = self._do_import_xml(new_xml, None, "section", task_name, "overwrite")
        return {
            "cloned_from": template_section, "new_section": new_section,
            "instance": instance_name, "block_type": block_type,
            "declared": decls, **result,
        }

    def _fb_interface(self, proj, fb_type: str):
        """Ordered (name, type) pins of a DFB: (inputs, outputs, inouts)."""
        item = self._find_in_collection(proj.Dfbs, fb_type, "DFB")
        xml = self._export_to_text(item, ".xdb", C.EXPORT_BASIC)

        def params(sec: str):
            m = re.search(rf"<{sec}>(.*?)</{sec}>", xml, re.S)
            return self._parse_variables(m.group(1)) if m else []

        return params("inputParameters"), params("outputParameters"), params("inOutParameters")

    def place_fb_in_ladder(
        self, task_name: str, section_name: str, fb_type: str, instance_name: str,
        bindings: dict[str, str], rung_input: str | None, rung_contact: str | None,
        pos_x: int = 2,
    ) -> dict:
        return self._run(
            self._do_place_fb_in_ladder, task_name, section_name, fb_type,
            instance_name, bindings, rung_input, rung_contact, pos_x,
        )

    def _do_place_fb_in_ladder(
        self, task_name, section_name, fb_type, instance_name, bindings,
        rung_input, rung_contact, pos_x,
    ) -> dict:
        """Generate (no template) a valid FFB-in-Ladder section for ANY DFB.

        Cracked LD-FFB geometry rule: a block spans height = max(#in,#out)+1 rows;
        each pin sits on its own row at the block edge; the block reaches the left
        rail by ONE boolean input wired on its pin-row (a contact -> col pos_x-1),
        EN left unbound, all other pins bound via effectiveParameter. The pin order
        in descriptionFFB is honoured by CE, so the wired input's row is its index.
        """
        proj = self._project(write=True)
        try:
            ins, outs, inouts = self._fb_interface(proj, fb_type)
        except CEError:
            raise CEError(
                f"Could not read interface of '{fb_type}'. place_fb_in_ladder supports "
                "project DFB types (read automatically). For elementary EFBs (TON, CTU, ...) "
                "author the block in FBD, or use a GUI template with use_fb_in_ladder."
            ) from None

        bool_ins = [n for n, t in ins if t.upper() in ("BOOL", "EBOOL")]
        if rung_input is None:
            if not bool_ins:
                raise CEError(
                    f"'{fb_type}' has no BOOL/EBOOL input to drive from the ladder rung. "
                    "Pass rung_input explicitly, or author the block in FBD."
                )
            rung_input = bool_ins[0]
        rung_contact = rung_contact or f"{instance_name}_EN"

        in_names = ["EN", rung_input] + [n for n, _ in ins if n != rung_input] + [n for n, _ in inouts]
        out_names = ["ENO"] + [n for n, _ in outs] + [n for n, _ in inouts]

        def pin(tag: str, name: str) -> str:
            if name in ("EN", "ENO") or name == rung_input or name not in bindings:
                eff = ""
            else:
                eff = f' effectiveParameter="{bindings[name]}"'
            return (f'\t\t\t\t\t\t\t<{tag} invertedPin="false" formalParameter="{name}"'
                    f"{eff}></{tag}>\n")

        desc = "".join(pin("inputVariable", n) for n in in_names) + \
               "".join(pin("outputVariable", n) for n in out_names)
        height = max(len(in_names), len(out_names)) + 1
        block = (
            f'<FFBBlock instanceName="{instance_name}" typeName="{fb_type}" '
            f'additionnalPinNumber="0" enEnO="true" width="10" height="{height}">\n'
            f'\t\t\t\t\t\t<objPosition posX="{pos_x}" posY="0"></objPosition>\n'
            f'\t\t\t\t\t\t<descriptionFFB execAfter="">\n{desc}\t\t\t\t\t\t</descriptionFFB>\n'
            "\t\t\t\t\t</FFBBlock>"
        )
        # rung_input is always at in_names index 1 (right after EN), so its pin
        # sits on the row directly below the block's top — the feed contact goes
        # on the very next typeLine, no spacer rows needed.
        rows = [f'\t\t\t\t<typeLine><emptyCell nbCells="{pos_x}"></emptyCell>{block}'
                f'<emptyCell nbCells="{11 - pos_x - 2}"></emptyCell></typeLine>']
        feed = (f'<contact typeContact="openContact" contactVariableName="{rung_contact}"></contact>'
                + (f'<HLink nbCells="{pos_x - 1}"></HLink>' if pos_x - 1 > 0 else ""))
        rows.append(f'\t\t\t\t<typeLine>{feed}<emptyCell nbCells="{11 - pos_x}"></emptyCell></typeLine>')
        rows.append('\t\t\t\t<typeLine><emptyLine nbRows="2"></emptyLine></typeLine>')

        decls = {instance_name: fb_type, rung_contact: "BOOL"}
        typemap = dict(ins + outs + inouts)
        for pinname, val in bindings.items():
            if re.fullmatch(r"[A-Za-z_]\w*", str(val)) and pinname in typemap:
                decls[val] = typemap[pinname]
        dblock = self._variable_rows(decls)
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<LDExchangeFile>\n'
            '\t<fileHeader company="Schneider Automation" product="Control Expert V14.0 - 190112" '
            'content="Ladder source file" DTDVersion="41"></fileHeader>\n'
            '\t<contentHeader name="Project" version="0.0.000"></contentHeader>\n\t<program>\n'
            f'\t\t<identProgram name="{section_name}" type="section" task="{task_name}"></identProgram>\n'
            '\t\t<LDSource nbColumns="11">\n\t\t\t<networkLD>\n'
            + "\n".join(rows) +
            '\n\t\t\t</networkLD>\n\t\t</LDSource>\n\t</program>\n'
            f'\t<dataBlock>\n{dblock}\t</dataBlock>\n</LDExchangeFile>\n'
        )
        result = self._do_import_xml(xml, None, "section", task_name, "overwrite")
        return {
            "section": section_name, "instance": instance_name, "block_type": fb_type,
            "rung_input": rung_input, "rung_contact": rung_contact,
            "height": height, "declared": decls, **result,
        }

    def export_xml(self, kind: str, task_name: str | None, name: str | None) -> dict:
        return self._run(self._do_export_xml, kind, task_name, name)

    def _do_export_xml(self, kind: str, task_name: str | None, name: str | None) -> dict:
        kind = kind.lower()
        proj = self._project(write=False)
        if kind == "variables":
            text = self._export_to_text(proj.Variables, ".xsy", C.EXPORT_VAR_ALL)
        elif kind == "program":
            text = self._export_to_text(proj.Program, ".xpg", C.EXPORT_BASIC)
        elif kind == "configuration":
            text = self._export_to_text(proj.Configuration, ".xhw", C.EXPORT_BASIC)
        elif kind == "dfb":
            if not name:
                raise CEError("kind='dfb' requires name.")
            coll = proj.Dfbs
            item = self._find_in_collection(coll, name, "DFB")
            text = self._export_to_text(item, ".xdb", C.EXPORT_BASIC)
        elif kind == "ddt":
            if not name:
                raise CEError("kind='ddt' requires name.")
            coll = proj.Ddts
            item = self._find_in_collection(coll, name, "DDT")
            text = self._export_to_text(item, ".xdd", C.EXPORT_BASIC)
        elif kind == "section":
            if not (task_name and name):
                raise CEError("kind='section' requires task_name and name.")
            task = self._find_task(proj, task_name)
            sec = self._find_section(task, name)
            text = self._export_to_text(sec, ".xpg", C.EXPORT_BASIC)
        else:
            raise CEError("Unknown kind. Use: variables, program, configuration, dfb, ddt, section.")

        if len(text.encode("utf-8", errors="ignore")) > MAX_INLINE_BYTES:
            path = self._temp_path(f"_{kind}.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return {"kind": kind, "too_large_inline": True, "file": path}
        return {"kind": kind, "xml": text}

    def _find_in_collection(self, coll, name: str, what: str):
        import win32com.client

        target = name.lower()
        try:
            item = self._wrap(self._get_prop(coll, "Item", name))
            if item is not None:
                return item
        except Exception:
            pass
        try:
            for item in coll:
                item = win32com.client.Dispatch(item)
                if str(item.Name).lower() == target:
                    return item
        except Exception:
            pass
        raise CEError(f"{what} '{name}' not found.")

    def export_project(self, path: str) -> dict:
        return self._run(self._do_export_project, path)

    def _do_export_project(self, path: str) -> dict:
        path = os.path.abspath(path)
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".xef", ".zef"):
            raise CEError("export_project path must end in .xef or .zef")
        proj = self._project(write=False)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        proj.Export(path, C.EXPORT_PROJECT_FULL)
        return {"exported": path}

    # ------------------------------------------------------------ public: UI

    def show_ui(self, state: str, mode: str = "read_only", command_line: str = "") -> dict:
        return self._run(self._do_show_ui, state, mode, command_line)

    def _do_show_ui(self, state: str, mode: str, command_line: str) -> dict:
        app = self._ensure_app()
        code = C.SHOW_STATES.get(state, C.SHOW_STATES["show_normal"])
        # DisplayStart fails in read-write when this automation client already
        # holds the write token (it almost always does). Read-only is also the
        # only mode in which <object>.DisplayEditor works — so it is the right
        # default for "let a human watch while the client drives".
        hmi = C.HMI_READ_WRITE if mode == "read_write" else C.HMI_READ_ONLY
        try:
            app.DisplayStart(hmi, command_line or "")
        except Exception:
            pass
        try:
            app.SetDisplayPosition(0, 0, 1280, 900, code)
            return {"visible": bool(int(app.IsVisible)), "mode": "read_write" if hmi else "read_only"}
        except Exception as exc:
            raise CEError(
                f"Could not show the Control Expert window: {_format_com_error(exc)}"
            ) from exc

    def open_animation_table(self, name: str, state: str = "show_normal") -> dict:
        return self._run(self._do_open_anim_table_editor, name, state)

    def _do_open_anim_table_editor(self, name: str, state: str) -> dict:
        """Open an animation table's editor inside the live Control Expert
        window. Connect to the simulator/PLC first (plc_connect) and the table
        then animates live values in the GUI. Per the UDE architecture doc,
        DisplayEditor requires the GUI started in READ-ONLY mode while a
        software client holds the write token."""
        app = self._ensure_app()
        table = self._find_anim_table(self._anim_tables(write=False), name)
        if table is None:
            raise CEError(
                f"Animation table '{name}' not found — create it with "
                "create_animation_table first."
            )
        try:
            app.DisplayStart(C.HMI_READ_ONLY, "")
        except Exception:
            pass
        try:
            self._get_prop(table, "DisplayEditor")
        except Exception as exc:
            raise CEError(
                f"Could not open the animation table editor: {_format_com_error(exc)}. "
                "The GUI must be in read-only mode (it is opened that way here)."
            ) from exc
        code = C.SHOW_STATES.get(state, C.SHOW_STATES["show_normal"])
        try:
            app.SetDisplayPosition(0, 0, 1280, 900, code)
        except Exception:
            pass
        visible = False
        try:
            visible = bool(int(app.IsVisible))
        except Exception:
            pass
        return {"opened_editor": name, "visible": visible,
                "note": "Connect (plc_connect) and the table animates live in the CE window."}

    # -------------------------------------------------------- public: online

    @staticmethod
    def _get_prop(obj, name: str, *args):
        """Invoke a COM property-get explicitly (DISPATCH_PROPERTYGET).

        Several UDE properties (Project, InternalBuses, DTMRoot, ...) fail
        through pywin32's dynamic dispatch because it invokes them as plain
        methods; raw PROPERTYGET invokes work everywhere. Dispatch results are
        wrapped so attribute access keeps working.
        """
        import pythoncom
        import win32com.client

        dispid = obj._oleobj_.GetIDsOfNames(0, name)
        result = obj._oleobj_.Invoke(
            dispid, 0, pythoncom.DISPATCH_PROPERTYGET | pythoncom.DISPATCH_METHOD, 1, *args
        )
        if isinstance(result, pythoncom.TypeIIDs[pythoncom.IID_IDispatch]):
            return win32com.client.Dispatch(result)
        return result

    @staticmethod
    def _put_property(obj, name: str, *args) -> None:
        """Set a parameterized COM property (e.g. PlcConnectionAddress(value, project)).

        Dynamic dispatch can't express parameterized property-puts via attribute
        assignment, so invoke DISPATCH_PROPERTYPUT directly. Argument order in
        the type library is (value, project); try the reverse as a fallback.
        """
        import pythoncom

        dispid = obj._oleobj_.GetIDsOfNames(0, name)
        flags = pythoncom.DISPATCH_PROPERTYPUT
        try:
            obj._oleobj_.Invoke(dispid, 0, flags, 0, *args)
        except Exception:
            obj._oleobj_.Invoke(dispid, 0, flags, 0, *reversed(args))

    def plc_setup_connection(self, target: str, address: str | None, driver: str | None) -> dict:
        return self._run(self._do_plc_setup, target, address, driver)

    def _do_plc_setup(self, target: str, address: str | None, driver: str | None) -> dict:
        app = self._ensure_app()
        proj = self._project(write=True)
        is_sim = target.lower() == "simulator"
        proj_ole = proj._oleobj_

        def put(setter: str, value: str) -> None:
            # The TLB defines Set* as propget-style members taking
            # (value, project) — invoke them like methods, not property-puts.
            try:
                self._get_prop(app, setter, value, proj_ole)
            except Exception:
                self._get_prop(app, setter, value)

        if address:
            put("SetSimulatorConnectionAddress" if is_sim else "SetPlcConnectionAddress", address)
        if driver:
            put("SetSimulatorConnectionDriver" if is_sim else "SetPlcConnectionDriver", driver)
        return {"target": target, "address": address, "driver": driver}

    def plc_connect(self, target: str, mode: str) -> dict:
        return self._run(self._do_plc_connect, target, mode)

    def _do_plc_connect(self, target: str, mode: str) -> dict:
        app = self._ensure_app()
        proj = self._project(write=True)
        mode_code = C.CONNECTION_MODE.get(mode, 1)
        target_code = C.TARGET_SIMULATOR if target.lower() == "simulator" else C.TARGET_PLC
        try:
            self._get_prop(app, "SetSelectTarget", target_code, proj._oleobj_)
        except Exception:
            pass
        app.TargetConnectionOpen(target_code, "", mode_code, proj)
        return self._do_plc_state()

    def plc_disconnect(self) -> dict:
        return self._run(self._do_plc_disconnect)

    def _do_plc_disconnect(self) -> dict:
        app = self._ensure_app()
        proj = self._project(write=False)
        app.TargetConnectionClose(proj)
        return {"connected": False}

    def plc_state(self) -> dict:
        return self._run(self._do_plc_state)

    def _do_plc_state(self) -> dict:
        app = self._ensure_app()
        out: dict[str, Any] = {}
        for key, getter in (
            ("connected", lambda: bool(int(app.IsTargetConnected))),
            ("connection_state", lambda: C.CONNECTION_STATES.get(int(app.TargetConnectionState))),
            ("plc_state", lambda: C.PLC_STATES.get(int(app.TargetState))),
            ("pc_equals_plc", lambda: bool(int(app.IsTargetConnectedEqual))),
        ):
            try:
                out[key] = getter()
            except Exception:
                pass
        return out

    def plc_transfer(self, direction: str) -> dict:
        return self._run(self._do_plc_transfer, direction)

    def _do_plc_transfer(self, direction: str) -> dict:
        app = self._ensure_app()
        if direction == "pc_to_plc":
            proj = self._project(write=True)
            proj.TransferAllPCtoPLC()
        elif direction == "plc_to_pc":
            app.TransferAllPLCToPC()
        else:
            raise CEError("direction must be 'pc_to_plc' or 'plc_to_pc'")
        return {"transferred": direction, **self._do_plc_state()}

    def plc_command(self, command: str) -> dict:
        return self._run(self._do_plc_command, command)

    def _do_plc_command(self, command: str) -> dict:
        code = C.PLC_COMMANDS.get(command.lower())
        if code is None:
            raise CEError(f"Unknown PLC command '{command}'. Use one of {sorted(C.PLC_COMMANDS)}.")
        app = self._ensure_app()
        proj = self._project(write=True)
        app.TargetSendCommand(code, proj)
        # The PLC state lags the command by a beat; re-read until it settles.
        import time

        target = {"run": "run", "stop": "stop", "init": "stop"}.get(command.lower())
        state = self._do_plc_state()
        for _ in range(10):
            if state.get("plc_state") == target:
                break
            time.sleep(0.3)
            state = self._do_plc_state()
        return {"command": command, **state}

    # ------------------------------------------------------------- QI helpers

    @staticmethod
    def _qi(obj, interface_name: str):
        """QueryInterface to a secondary dual interface, wrapped as IDispatch
        so late-bound member access resolves against that interface."""
        import pythoncom
        import pywintypes
        import win32com.client

        iid = pywintypes.IID(IIDS[interface_name])
        disp = obj._oleobj_.QueryInterface(iid, pythoncom.IID_IDispatch)
        return win32com.client.Dispatch(disp)

    @staticmethod
    def _wrap(value):
        """Wrap a raw PyIDispatch (returned from [out] params) for attribute access."""
        import pythoncom
        import win32com.client

        if isinstance(value, pythoncom.TypeIIDs[pythoncom.IID_IDispatch]):
            return win32com.client.Dispatch(value)
        return value

    def _iter_qi(self, coll, interface_name: str):
        """Iterate a hardware/DTM collection, QI'ing each item."""
        import win32com.client

        for item in coll:
            yield self._qi(win32com.client.Dispatch(item), interface_name)

    def _invoke_out(self, obj, name: str, in_args: tuple = (), out_vts: tuple = ()):
        """Invoke a member that has [out] parameters (no [out, retval]).

        Raw IDispatch::Invoke must receive a by-ref VARIANT placeholder for
        every [out] parameter or the server rejects the call with
        DISP_E_BADPARAMCOUNT ('Invalid number of parameters').
        Returns the single out-value, or a list when there are several.
        """
        import pythoncom
        from win32com.client import VARIANT

        outs = [VARIANT(pythoncom.VT_BYREF | vt, None) for vt in out_vts]
        dispid = obj._oleobj_.GetIDsOfNames(0, name)
        obj._oleobj_.Invoke(
            dispid, 0,
            pythoncom.DISPATCH_PROPERTYGET | pythoncom.DISPATCH_METHOD, 1,
            *in_args, *outs,
        )
        vals = [self._wrap(o.value) for o in outs]
        if not vals:
            return None
        return vals[0] if len(vals) == 1 else vals

    # --------------------------------------------------------------- hardware

    def _buses(self):
        proj = self._project(write=True)
        conf2 = self._qi(proj.Configuration, "IConfiguration2")
        return self._wrap(self._get_prop(conf2, "InternalBuses"))

    @staticmethod
    def _hw_info(obj) -> dict:
        info = {}
        for key in ("Name", "PartNumber", "Version", "TopoNumber", "TopoAddress"):
            try:
                val = getattr(obj, key)
                if callable(val):
                    val = val()
                if val not in (None, ""):
                    info[key.lower()] = val
            except Exception:
                pass
        return info

    def get_hardware(self) -> dict:
        return self._run(self._do_get_hardware)

    def _do_get_hardware(self) -> dict:
        proj = self._project(write=True)
        out: dict[str, Any] = {}
        try:
            cpu = proj.Configuration.Cpu
            out["cpu"] = {
                "family": str(cpu.Family),
                "part_number": str(cpu.PartNumber),
                "version": str(cpu.Version),
                "topo_address": str(cpu.TopoAddress),
            }
        except Exception:
            pass
        buses_out = []
        try:
            buses = self._buses()
        except Exception as exc:  # noqa: BLE001 - e.g. SCADAPack/RemoteConnect logic: no CE rack tree
            out["buses"] = []
            out["note"] = (
                "This project has no Control Expert bus/rack configuration "
                f"({_format_com_error(exc)}). That is normal for OEM logic projects such as "
                "RemoteConnect SCADAPack x70, whose I/O lives in the RemoteConnect configuration "
                "(see inventory_remoteconnect_configuration / export_remoteconnect_configuration "
                "when available).")
            del exc
            return out
        for bus in self._iter_qi(buses, "IBus"):
            bus_info = self._hw_info(bus)
            drops_out = []
            try:
                drops = self._wrap(self._get_prop(bus, "Drops"))
                for drop in self._iter_qi(drops, "IDrop"):
                    drop_info = self._hw_info(drop)
                    racks_out = []
                    racks = self._wrap(self._get_prop(drop, "Racks"))
                    for rack in self._iter_qi(racks, "IRack"):
                        rack_info = self._hw_info(rack)
                        mods_out = []
                        modules = self._wrap(self._get_prop(rack, "Modules"))
                        for mod in self._iter_qi(modules, "IModule"):
                            mods_out.append(self._hw_info(mod))
                        rack_info["modules"] = mods_out
                        racks_out.append(rack_info)
                    drop_info["racks"] = racks_out
                    drops_out.append(drop_info)
            except Exception as exc:
                bus_info["drops_error"] = _format_com_error(exc)
                del exc
            bus_info["drops"] = drops_out
            buses_out.append(bus_info)
        out["buses"] = buses_out
        return out

    def _local_modules(self, bus_name: str | None, drop_topo: int | None, rack_topo: int):
        """Navigate to the Modules collection of a rack (defaults: local bus,
        local drop, rack 0 / local rack)."""
        bus = self._find_bus(bus_name)
        drops = self._wrap(self._get_prop(bus, "Drops"))
        drop = None
        if drop_topo is not None:
            for cand in self._iter_qi(drops, "IDrop"):
                topo = cand.TopoNumber() if callable(cand.TopoNumber) else cand.TopoNumber
                if int(topo) == drop_topo:
                    drop = cand
                    break
            if drop is None:
                raise CEError(f"Drop {drop_topo} not found (see get_hardware).")
        else:
            drop = self._qi(self._wrap(self._get_prop(drops, "LocalChild")), "IDrop")
        racks = self._wrap(self._get_prop(drop, "Racks"))
        rack = None
        for cand in self._iter_qi(racks, "IRack"):
            topo = cand.TopoNumber() if callable(cand.TopoNumber) else cand.TopoNumber
            if int(topo) == rack_topo:
                rack = cand
                break
        if rack is None:
            try:
                rack = self._qi(self._wrap(self._get_prop(racks, "LocalChild")), "IRack")
            except Exception:
                raise CEError(f"Rack {rack_topo} not found (see get_hardware).") from None
        return self._wrap(self._get_prop(rack, "Modules"))

    def add_io_module(
        self,
        part_number: str,
        slot: int,
        version: str,
        rack_topo: int,
        drop_topo: int | None,
        bus_name: str | None,
    ) -> dict:
        return self._run(
            self._do_add_io_module, part_number, slot, version, rack_topo, drop_topo, bus_name
        )

    def _do_add_io_module(self, part_number, slot, version, rack_topo, drop_topo, bus_name) -> dict:
        if not version:
            raise CEError(
                "version is required and must match the module's catalog version — "
                "'02.00' for most M340 IO modules, '01.00' for racks/power supplies. "
                "On failure, try '02.00' then '01.00'."
            )
        modules = self._local_modules(bus_name, drop_topo, rack_topo)
        try:
            added = self._wrap(self._get_prop(modules, "AddChild", slot, 0, part_number, version or ""))
        except CEError:
            raise
        except Exception as exc:
            msg = _format_com_error(exc)
            del exc
            raise CEError(
                f"AddChild failed for '{part_number}' v'{version}' at slot {slot}: {msg}. "
                "'Catalog object not found' = part number/version not in the hardware "
                "catalog for this PLC family; 'not insertable' = slot occupied or module "
                "not allowed at that position."
            ) from None
        mod = self._qi(added, "IModule")
        return {"added": self._hw_info(mod)}

    def replace_io_module(
        self,
        slot: int,
        old_part_number: str,
        old_version: str,
        new_part_number: str,
        new_version: str,
        rack_topo: int,
        drop_topo: int | None,
        bus_name: str | None,
    ) -> dict:
        return self._run(
            self._do_replace_io_module, slot, old_part_number, old_version,
            new_part_number, new_version, rack_topo, drop_topo, bus_name,
        )

    def _do_replace_io_module(self, slot, old_pn, old_ver, new_pn, new_ver,
                              rack_topo, drop_topo, bus_name) -> dict:
        # Modules.ReplaceChild is rack-only ('service not applicable here'),
        # so module replacement = DeleteChild + AddChild.
        modules = self._local_modules(bus_name, drop_topo, rack_topo)
        try:
            self._get_prop(modules, "DeleteChild", slot, 0)
        except Exception as exc:
            raise self._module_delete_error(exc, "replace", slot, drop_topo) from None
        added = self._wrap(self._get_prop(modules, "AddChild", slot, 0, new_pn, new_ver))
        return {"replaced": self._hw_info(self._qi(added, "IModule"))}

    @staticmethod
    def _module_delete_error(exc: Exception, action: str, slot: int, drop_topo) -> "CEError":
        """Wrap an IModules.DeleteChild failure with an actionable hint. On a
        remote EIO/X80 drop rack DeleteChild is rejected ('service not
        applicable here') even though AddChild works there — a headless-API
        limitation, not a slot mistake."""
        msg = _format_com_error(exc)
        if "not applicable" in msg.lower():
            where = "a remote EIO/X80 drop rack" if drop_topo not in (None, 0) else "this rack"
            return CEError(
                f"Cannot {action} the module at slot {slot}: {msg}. Removing or "
                f"replacing modules on {where} is not supported by the headless "
                "automation API (IModules.DeleteChild is rejected there) — AddChild "
                "works, but delete/replace do not. Workaround: delete and re-create "
                "the whole drop, or place the correctly-sized module (e.g. an adequate "
                "power supply) when first populating the rack."
            )
        return CEError(f"Cannot {action} the module at slot {slot}: {msg}.")

    def replace_rack(
        self,
        rack_topo: int,
        old_part_number: str,
        old_version: str,
        new_part_number: str,
        new_version: str,
        drop_topo: int | None,
        bus_name: str | None,
    ) -> dict:
        return self._run(
            self._do_replace_rack, rack_topo, old_part_number, old_version,
            new_part_number, new_version, drop_topo, bus_name,
        )

    def _do_replace_rack(self, rack_topo, old_pn, old_ver, new_pn, new_ver,
                         drop_topo, bus_name) -> dict:
        bus = self._find_bus(bus_name)
        drops = self._wrap(self._get_prop(bus, "Drops"))
        if drop_topo is not None:
            drop = None
            for cand in self._iter_qi(drops, "IDrop"):
                topo = cand.TopoNumber() if callable(cand.TopoNumber) else cand.TopoNumber
                if int(topo) == drop_topo:
                    drop = cand
                    break
            if drop is None:
                raise CEError(f"Drop {drop_topo} not found on bus '{bus_name}'.")
        else:
            drop = self._qi(self._wrap(self._get_prop(drops, "LocalChild")), "IDrop")
        # dynamic dispatch: the trailing IRack** is [out,retval]
        racks = self._wrap(self._get_prop(drop, "Racks"))
        new_rack = racks.ReplaceChild(rack_topo, 0, old_pn, old_ver, new_pn, new_ver)
        return {"replaced_rack": self._hw_info(self._qi(self._wrap(new_rack), "IRack"))}

    def remove_io_module(
        self, slot: int, rack_topo: int, drop_topo: int | None, bus_name: str | None
    ) -> dict:
        return self._run(self._do_remove_io_module, slot, rack_topo, drop_topo, bus_name)

    def _do_remove_io_module(self, slot, rack_topo, drop_topo, bus_name) -> dict:
        modules = self._local_modules(bus_name, drop_topo, rack_topo)
        try:
            self._get_prop(modules, "DeleteChild", slot, 0)
        except Exception as exc:
            raise self._module_delete_error(exc, "remove", slot, drop_topo) from None
        return {"removed_slot": slot, "rack": rack_topo}

    def _find_bus(self, bus_name: str | None):
        buses = self._buses()
        if not bus_name:
            return self._qi(self._wrap(self._get_prop(buses, "LocalChild")), "IBus")
        needle = bus_name.lower()
        for cand in self._iter_qi(buses, "IBus"):
            if needle in str(cand.Name).lower():
                return cand
        raise CEError(f"Bus '{bus_name}' not found (see get_hardware for bus names).")

    def add_drop(self, bus_name: str, drop_topo: int, part_number: str, version: str) -> dict:
        return self._run(self._do_add_drop, bus_name, drop_topo, part_number, version)

    def _do_add_drop(self, bus_name, drop_topo, part_number, version) -> dict:
        bus = self._find_bus(bus_name)
        drops = self._wrap(self._get_prop(bus, "Drops"))
        drop = self._qi(
            self._wrap(self._get_prop(drops, "AddChild", drop_topo, 0, part_number, version or "01.00")),
            "IDrop",
        )
        return {"added_drop": self._hw_info(drop), "bus": str(bus.Name)}

    def add_rack(
        self, bus_name: str, drop_topo: int, rack_topo: int, part_number: str, version: str
    ) -> dict:
        return self._run(self._do_add_rack, bus_name, drop_topo, rack_topo, part_number, version)

    def _do_add_rack(self, bus_name, drop_topo, rack_topo, part_number, version) -> dict:
        bus = self._find_bus(bus_name)
        drops = self._wrap(self._get_prop(bus, "Drops"))
        drop = None
        for cand in self._iter_qi(drops, "IDrop"):
            topo = cand.TopoNumber() if callable(cand.TopoNumber) else cand.TopoNumber
            if int(topo) == drop_topo:
                drop = cand
                break
        if drop is None:
            raise CEError(f"Drop {drop_topo} not found on bus '{bus_name}'.")
        racks = self._wrap(self._get_prop(drop, "Racks"))
        rack = self._qi(
            self._wrap(self._get_prop(racks, "AddChild", rack_topo, 0, part_number, version or "01.00")),
            "IRack",
        )
        return {"added_rack": self._hw_info(rack), "drop": str(drop.Name)}

    def change_cpu(self, part_number: str, version: str) -> dict:
        return self._run(self._do_change_cpu, part_number, version)

    def _do_change_cpu(self, part_number: str, version: str) -> dict:
        proj = self._project(write=True)
        self._get_prop(proj.Configuration, "Change", part_number, version)
        cpu = proj.Configuration.Cpu
        return {"cpu": {"part_number": str(cpu.PartNumber), "version": str(cpu.Version)}}

    # -------------------------------------------------------------------- DTM

    def _dtm_root(self):
        proj3 = self._qi(self._project(write=True), "IProject3")
        return self._qi(self._wrap(self._get_prop(proj3, "DTMRoot")), "IPServerDtmRoot")

    def _iter_dtm_coll(self, coll):
        try:
            yield from self._iter_qi(coll, "IPServerDtm")
        except Exception:
            return

    def _dtm_info(self, dtm, depth: int = 0) -> dict:
        info: dict[str, Any] = {}
        for key, attr in (("name", "AliasName"), ("dtm_id", "DtmId"), ("type", "DtmType"),
                          ("address", "SlaveBusAddress")):
            try:
                val = getattr(dtm, attr)
                if callable(val):
                    val = val()
                if val not in (None, ""):
                    info[key] = val
            except Exception:
                pass
        if depth < 4:
            children = []
            try:
                sub = self._qi(self._wrap(self._get_prop(dtm, "SubLstDtms")), "IPServerDtms")
                for child in self._iter_dtm_coll(sub):
                    children.append(self._dtm_info(child, depth + 1))
            except Exception:
                pass
            if children:
                info["children"] = children
        return info

    def list_dtms(self) -> dict:
        return self._run(self._do_list_dtms)

    def _do_list_dtms(self) -> dict:
        root = self._dtm_root()
        coll = self._qi(self._wrap(root.Dtms()), "IPServerDtms")
        out = [self._dtm_info(d) for d in self._iter_dtm_coll(coll)]
        return {"dtms": out}

    def _find_dtm(self, name: str):
        import pythoncom

        root = self._dtm_root()
        try:
            found = self._invoke_out(root, "GetDtmFromName", (name,), (pythoncom.VT_DISPATCH,))
            if found is not None:
                return self._qi(found, "IPServerDtm")
        except Exception:
            pass

        def walk(coll):
            for dtm in self._iter_dtm_coll(coll):
                try:
                    alias = dtm.AliasName() if callable(dtm.AliasName) else dtm.AliasName
                except Exception:
                    alias = None
                if alias and str(alias).lower() == name.lower():
                    return dtm
                try:
                    sub = self._qi(self._wrap(self._get_prop(dtm, "SubLstDtms")), "IPServerDtms")
                    hit = walk(sub)
                    if hit is not None:
                        return hit
                except Exception:
                    continue
            return None

        coll = self._qi(self._wrap(root.Dtms()), "IPServerDtms")
        hit = walk(coll)
        if hit is None:
            raise CEError(f"DTM '{name}' not found (see list_dtms).")
        return hit

    def add_dtm(
        self,
        device_type_name: str,
        dtm_name: str,
        parent_dtm: str | None,
        protocol_id: str | None,
        prog_id: str | None,
        version: str | None,
    ) -> dict:
        return self._run(
            self._do_add_dtm, device_type_name, dtm_name, parent_dtm, protocol_id, prog_id, version
        )

    def _do_add_dtm(self, device_type_name, dtm_name, parent_dtm, protocol_id, prog_id, version) -> dict:
        import pythoncom

        root = self._dtm_root()
        try:
            if parent_dtm:
                parent = self._find_dtm(parent_dtm)
                sub = self._qi(self._wrap(self._get_prop(parent, "SubLstDtms")), "IPServerDtms")
                # The protocol id is the FDT protocolSpecificName ('Modbus' for
                # the generic Modbus TCP DTM). When not given, try common ones.
                protocols = [protocol_id] if protocol_id else ["Modbus", "EtherNet/IP", "EtherNetIP"]
                last_err: str | None = None
                for proto in protocols:
                    try:
                        new = self._invoke_out(
                            sub, "Add",
                            (proto, device_type_name, prog_id or "",
                             version or "", dtm_name, False),
                            (pythoncom.VT_DISPATCH,),
                        )
                        dtm = self._qi(new, "IPServerDtm")
                        return {"added": self._dtm_info(dtm), "parent": parent_dtm,
                                "protocol_id": proto}
                    except Exception as exc:  # noqa: BLE001
                        last_err = _format_com_error(exc)
                        del exc
                raise CEError(last_err or "Add failed")
            dtm_id = self._invoke_out(
                root, "AddCommunicationDtm",
                (device_type_name, prog_id or "", version or "", dtm_name, False),
                (pythoncom.VT_BSTR,),
            )
            return {"added": dtm_name, "dtm_id": dtm_id}
        except CEError:
            raise
        except Exception as exc:
            msg = _format_com_error(exc)
            del exc
            raise CEError(
                f"add_dtm failed for device type '{device_type_name}': {msg}. The device "
                "type name must exactly match an entry of the Control Expert DTM hardware "
                "catalog (e.g. 'Modbus Device', 'BMEP58_ECPU_EXT', 'Generic Device'). For "
                "slave devices pass parent_dtm (the master/communication DTM) and, if "
                "needed, protocol_id (e.g. 'ModbusTCP')."
            ) from None

    def delete_dtm(self, name: str) -> dict:
        return self._run(self._do_delete_dtm, name)

    def _do_delete_dtm(self, name: str) -> dict:
        dtm = self._find_dtm(name)
        self._get_prop(dtm, "Delete")
        return {"deleted": name}

    def set_dtm_address(
        self, name: str, address: str,
        gateway: str | None = None, subnet: str | None = None,
    ) -> dict:
        return self._run(self._do_set_dtm_address, name, address, gateway, subnet)

    def _do_set_dtm_address(self, name, address, gateway=None, subnet=None) -> dict:
        out: dict[str, Any] = {"dtm": name}
        address_via_dataset = False
        if address:
            dtm = self._find_dtm(name)
            try:
                self._put_property(dtm, "SlaveBusAddress", address)
                out["address"] = address
            except Exception as exc:
                # Some device DTMs (e.g. STB NIP2x1x islands) reject the FDT
                # SlaveBusAddress property — fall back to patching the address
                # in the master dataset / device list (same path as gateway).
                del exc
                address_via_dataset = True
        if not (gateway or subnet or address_via_dataset):
            return out
        out.update(self._do_set_slave_gateway(
            name, gateway, subnet, address if address_via_dataset else None))
        if address_via_dataset:
            out["address"] = address
            out["address_via_dataset"] = True
        return out

    def _do_set_slave_gateway(self, name, gateway, subnet, address=None) -> dict:
        """Slave gateway/subnet have no FDT property surface; the build's
        'IP Address and Gateway address are not in the same domain' check reads
        the application's Ethernet device list (unitpro.xef), so patch every
        stored copy via a ZEF round-trip (device list, master dataset Topology,
        CPU channel paramKPW device table)."""
        _mname, master_id = self._find_dtm_master()
        ds = self._do_get_master_dataset(None)
        dataset = ds.get("xml") or _read_text_file(ds["file"])
        blk = re.search(
            rf'<SlaveDevice deviceTag="{re.escape(name)}".*?</SlaveDevice>',
            dataset, re.S)
        if not blk:
            raise CEError(
                f"Slave device '{name}' not found in the master DTM dataset Topology.")
        old_gw = re.search(r'slaveDeviceGatewayID="([^"]*)"', blk.group(0)).group(1)
        old_mask = re.search(r'slaveDeviceSubnetID="([^"]*)"', blk.group(0)).group(1)
        old_addr_m = re.search(r'slaveDeviceAddressID="([^"]*)"', blk.group(0))
        old_addr = old_addr_m.group(1) if old_addr_m else None
        new_gw = gateway or old_gw
        new_mask = subnet or old_mask
        new_addr = address or old_addr

        def patch_master_bin(data: bytes) -> bytes:
            text, marker = self._u16_decode(data)

            def fix(m):
                seg = m.group(0)
                seg = re.sub(r'(slaveDeviceGatewayID=")[^"]*(")',
                             rf"\g<1>{new_gw}\g<2>", seg)
                seg = re.sub(r'(slaveDeviceSubnetID=")[^"]*(")',
                             rf"\g<1>{new_mask}\g<2>", seg)
                if address:
                    seg = re.sub(r'(slaveDeviceAddressID=")[^"]*(")',
                                 rf"\g<1>{new_addr}\g<2>", seg)
                    # EtherNet/IP CIP node identifier also carries the IP
                    seg = re.sub(r'(<ExtendedIdentifier extendedIdentifier=")[^"]*(")',
                                 rf"\g<1>{new_addr}\g<2>", seg)
                return seg
            text = re.sub(
                rf'<SlaveDevice deviceTag="{re.escape(name)}".*?</SlaveDevice>',
                fix, text, flags=re.S)
            return self._u16_encode(text, marker)

        def patch_xef(data: bytes) -> bytes:
            text = data.decode("utf-8")
            m = re.search(rf'<\w+ [^>]*name="{re.escape(name)}"[^>]*\bgateway="[^>]*>',
                          text)
            if m:
                seg = m.group(0)
                seg = re.sub(r'(\bgateway=")[^"]*(")', rf"\g<1>{new_gw}\g<2>", seg)
                seg = re.sub(r'(\bsubnetMask=")[^"]*(")', rf"\g<1>{new_mask}\g<2>", seg)
                if address:
                    seg = re.sub(r'(\bipAddress=")[^"]*(")', rf"\g<1>{new_addr}\g<2>", seg)
                text = text[: m.start()] + seg + text[m.end():]
            # CPU channel device table: gateway+mask byte pair (little-endian)
            km = re.search(
                r'(<channelATS ASFCatKey="RIODIOBML2"[^>]*>.*?<paramKPW>)'
                r"(.*?)(</paramKPW>)", text, re.S)
            if km:
                vals = [int(v) for v in re.findall(r'hexaValue="(\d+)"', km.group(2))]
                old_seq = self._ip_le_bytes(old_gw) + self._ip_le_bytes(old_mask)
                new_seq = self._ip_le_bytes(new_gw) + self._ip_le_bytes(new_mask)
                for i in range(374, len(vals) - 7):
                    if vals[i: i + 8] == old_seq:
                        vals[i: i + 8] = new_seq
                body = "".join(f'<hexaValue hexaValue="{v}"></hexaValue>' for v in vals)
                text = text[: km.start()] + km.group(1) + body + km.group(3) + text[km.end():]
            return text.encode("utf-8")

        result = self._do_zef_patch({
            "unitpro.xef": patch_xef,
            f"DTM/BinaryFile/{master_id}.bin": patch_master_bin,
        })
        out = {"gateway": new_gw, "subnet": new_mask, **result,
               "note": "Project reloaded from the patched archive and UNSAVED — "
                       "run build_project to validate, then save_project."}
        if address:
            out["address"] = new_addr
        return out

    def get_dtm_control_parameters(self, name: str) -> dict:
        return self._run(self._do_get_dtm_control, name)

    def _do_get_dtm_control(self, name: str) -> dict:
        import pythoncom

        dtm = self._find_dtm(name)
        xml, app_ok = self._invoke_out(
            dtm, "GetControlParameter", (), (pythoncom.VT_BSTR, pythoncom.VT_BOOL)
        )
        return {"dtm": name, "xml": xml, "application_ok": bool(app_ok)}

    def set_dtm_control_parameters(self, name: str, xml: str, build: bool) -> dict:
        return self._run(self._do_set_dtm_control, name, xml, build)

    def _do_set_dtm_control(self, name: str, xml: str, build: bool) -> dict:
        import pythoncom

        dtm = self._find_dtm(name)
        app_ok = self._invoke_out(dtm, "SetControlParameter", (xml,), (pythoncom.VT_BOOL,))
        out: dict[str, Any] = {"dtm": name, "set": True, "application_ok": bool(app_ok)}
        if build:
            # Establish the DTM<->PLC link before building control information
            try:
                self._get_prop(dtm, "UpdateDtmPlcLink", False)
                out["plc_link_updated"] = True
            except Exception as exc:
                out["link_error"] = _format_com_error(exc)
                del exc
            try:
                built_ok = self._invoke_out(
                    dtm, "BuildControlInformation", (), (pythoncom.VT_BOOL,)
                )
                out["control_information_built"] = bool(built_ok)
            except Exception as exc:
                out["build_error"] = _format_com_error(exc)
                del exc
        return out

    def get_dtm_dataset(self, name: str) -> dict:
        return self._run(self._do_get_dtm_dataset, name)

    def _do_get_dtm_dataset(self, name: str) -> dict:
        import pythoncom

        dtm = self._find_dtm(name)
        data = self._invoke_out(dtm, "ExportConfiguration", (), (pythoncom.VT_VARIANT,))
        raw = bytes(bytearray(data)) if not isinstance(data, (bytes, bytearray)) else bytes(data)
        text = None
        # ExportConfiguration returns a binary-framed FDT dataset container, NOT a
        # clean XML document: a small length/GUID/type header wraps UTF-16LE XML,
        # so ~half the bytes are NUL. Decoding the whole buffer as UTF-8 *succeeds*
        # but yields NUL-interleaved garbage; pick UTF-16 by NUL ratio instead (a
        # bare raw[1]==0 test misses headers like b"\\\r\x00\x00..." on IO racks).
        nul_ratio = (raw.count(0) / len(raw)) if raw else 0.0
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16", errors="replace")
        elif nul_ratio > 0.2 and len(raw) % 2 == 0:
            text = raw.decode("utf-16-le", errors="replace")
        else:
            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
        lt = text.find("<") if text else -1
        if lt < 0:
            path = self._temp_path(f"_{name}_dataset.bin")
            with open(path, "wb") as f:
                f.write(raw)
            return {"dtm": name, "binary": True, "file": path, "size": len(raw)}
        # Strip the binary framing before the first '<'. The result is the
        # embedded XML, readable for inspection but NOT byte-faithful, so it is
        # not safely round-trippable through set_dtm_dataset.
        xml_text = text[lt:]
        framed = lt > 0 or "\x00" in xml_text
        result: dict[str, Any] = {"dtm": name}
        if framed:
            result["note"] = (
                "This DTM dataset is a binary-framed FDT container; the returned "
                "'xml' has the framing stripped for reading and is NOT byte-faithful "
                "— do not round-trip it through set_dtm_dataset. For M580 Modbus scan "
                "lines use get_master_dtm_dataset / set_master_dtm_dataset instead."
            )
        if len(xml_text.encode("utf-8", errors="ignore")) > MAX_INLINE_BYTES:
            path = self._temp_path(f"_{name}_dataset.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml_text)
            result["too_large_inline"] = True
            result["file"] = path
            return result
        result["xml"] = xml_text
        return result

    def set_dtm_dataset(self, name: str, xml: str) -> dict:
        return self._run(self._do_set_dtm_dataset, name, xml)

    def _do_set_dtm_dataset(self, name: str, xml: str) -> dict:
        import pythoncom
        from win32com.client import VARIANT

        dtm = self._find_dtm(name)
        payload = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, xml.encode("utf-8"))
        self._get_prop(dtm, "ImportConfiguration", payload)
        return {"dtm": name, "imported": True}

    # -------------------------------------------------- animation tables

    def _anim_tables(self, write: bool = True):
        proj = self._project(write=write)
        return self._wrap(self._get_prop(proj, "AnimationTables"))

    def list_animation_tables(self) -> dict:
        return self._run(self._do_list_anim_tables)

    def _do_list_anim_tables(self) -> dict:
        import win32com.client

        out = []
        coll = self._anim_tables(write=False)
        try:
            for t in coll:
                td = win32com.client.Dispatch(t)
                entry = {"name": str(td.Name)}
                try:
                    entry["temporary"] = bool(self._get_prop(td, "GetTemporary"))
                except Exception:
                    pass
                out.append(entry)
        except Exception as exc:
            return {"animation_tables": out, "error": _format_com_error(exc)}
        return {"animation_tables": out}

    def create_animation_table(self, name: str, variables: list[str]) -> dict:
        return self._run(self._do_create_anim_table, name, variables)

    def _find_anim_table(self, coll, name: str):
        import win32com.client

        for t in coll:
            td = win32com.client.Dispatch(t)
            if str(td.Name).lower() == name.lower():
                return td
        return None

    def _do_create_anim_table(self, name: str, variables: list[str]) -> dict:
        import pythoncom
        from win32com.client import VARIANT

        coll = self._anim_tables(write=True)
        table = self._find_anim_table(coll, name)
        created = False
        if table is None:
            # Add(name, pRootNode) — root node is an IUnknown; pass a typed NULL
            null_unk = VARIANT(pythoncom.VT_UNKNOWN, None)
            table = self._wrap(self._get_prop(coll, "Add", name, null_unk))
            created = True
        added, errors = [], []
        for var in variables:
            try:
                self._get_prop(table, "AddVariable", var)
                added.append(var)
            except Exception as exc:
                errors.append(f"{var}: {_format_com_error(exc)}")
                del exc
        result: dict[str, Any] = {"table": name, "created": created, "added": added}
        if errors:
            result["errors"] = errors
        return result

    def delete_animation_table(self, name: str) -> dict:
        return self._run(self._do_delete_anim_table, name)

    def _do_delete_anim_table(self, name: str) -> dict:
        coll = self._anim_tables(write=True)
        table = self._find_anim_table(coll, name)
        if table is None:
            raise CEError(f"Animation table '{name}' not found.")
        self._get_prop(table, "Delete")
        return {"deleted": name}

    # ------------------------------------------- master DTM dataset via ZEF

    def _zef_locate_dataset(self, zef_path: str, dtm_label: str | None):
        """Find the dataset bin of a DTM node inside a .zef archive.

        Returns (bin_name, text, encoding). The CPU/communication DTM dataset
        is where M580 Modbus scan lines live (<SlaveDevices> /
        <ManagedModbusRequestList>), per the 'M580 DTMs XML Dataset' spec.
        """
        import zipfile

        with zipfile.ZipFile(zef_path) as z:
            names = z.namelist()
            topo_name = next((n for n in names if n.endswith("FDTDTMTopology.xml")), None)
            if topo_name is None:
                raise CEError("The project has no DTM topology (no DTMs in this project).")
            topo_raw = z.read(topo_name)
            topo = topo_raw.decode("utf-16" if topo_raw[:1] == b"<" and topo_raw[1:2] == b"\x00" else "utf-8",
                                   errors="replace")
            nodes = re.findall(r'<DTMNode\s+identifier="([0-9a-fA-F-]+)"\s+label="([^"]+)"', topo)
            if not nodes:
                raise CEError("No DTMNode entries found in FDTDTMTopology.xml.")
            guid = None
            if dtm_label:
                for g, label in nodes:
                    if label.lower() == dtm_label.lower():
                        guid = g
                        break
                if guid is None:
                    raise CEError(
                        f"DTM '{dtm_label}' not found in topology. Available: "
                        + ", ".join(label for _, label in nodes)
                    )
            else:
                guid = nodes[0][0]  # top-level node = master/communication DTM
            bin_name = next(
                (n for n in names if n.endswith(f"BinaryFile/{guid}.bin")), None
            )
            if bin_name is None:
                raise CEError(f"Dataset file for DTM id {guid} not found in the archive.")
            raw = z.read(bin_name)
            if raw[1:2] == b"\x00":
                return bin_name, raw.decode("utf-16-le"), "utf-16-le"
            return bin_name, raw.decode("utf-8", errors="replace"), "utf-8"

    def get_master_dtm_dataset(self, dtm_label: str | None) -> dict:
        return self._run(self._do_get_master_dataset, dtm_label)

    def _do_get_master_dataset(self, dtm_label: str | None) -> dict:
        zef = self._temp_path(".zef")
        proj = self._project(write=True)
        try:
            proj.Export(zef, C.EXPORT_PROJECT_FULL)
            bin_name, text, _enc = self._zef_locate_dataset(zef, dtm_label)
        finally:
            try:
                os.remove(zef)
            except OSError:
                pass
        if len(text.encode("utf-8", errors="ignore")) > MAX_INLINE_BYTES:
            path = self._temp_path("_master_dataset.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return {"dataset_entry": bin_name, "too_large_inline": True, "file": path}
        return {"dataset_entry": bin_name, "xml": text}

    def set_master_dtm_dataset(self, xml: str, dtm_label: str | None) -> dict:
        return self._run(self._do_set_master_dataset, xml, dtm_label)

    def _do_set_master_dataset(self, xml: str, dtm_label: str | None) -> dict:
        import zipfile

        proj = self._project(write=True)
        orig_path = self._project_path  # restore on success/rollback
        zef = self._temp_path(".zef")
        new_zef = self._temp_path("_mod.zef")
        proj.Export(zef, C.EXPORT_PROJECT_FULL)
        del proj
        try:
            bin_name, _old, enc = self._zef_locate_dataset(zef, dtm_label)
            with zipfile.ZipFile(zef) as src, zipfile.ZipFile(
                new_zef, "w", zipfile.ZIP_DEFLATED
            ) as dst:
                for item in src.infolist():
                    data = src.read(item.filename)
                    if item.filename == bin_name:
                        data = xml.encode(enc)
                    dst.writestr(item, data)
            # Reload from the modified archive; roll back to the untouched export
            # if the dataset is rejected, so the caller keeps an open project.
            self._reimport_zef(
                new_zef, zef, orig_path,
                "set_master_dtm_dataset failed: the modified DTM dataset was "
                "rejected ({error}). Return the ENTIRE dataset byte-faithfully and "
                "change ONLY the <ManagedModbusRequestList> contents — trimming or "
                "reordering other sections is rejected. The original project has "
                "been restored (unsaved).",
            )
            self._project_path = orig_path
            self._needs_saveas = orig_path is not None
            return {
                "dataset_entry": bin_name,
                "reloaded": True,
                "note": (
                    "Project was reloaded from the modified ZEF and is unsaved — "
                    "run build_project to validate the scan lines, then save_project."
                ),
            }
        finally:
            for p in (zef, new_zef):
                try:
                    os.remove(p)
                except OSError:
                    pass

    # ------------------------------------------ CPU Ethernet / security cfg

    # The M580 CPU embedded-Ethernet security services are a 16-bit mask. The
    # same word lives in three places that must stay in sync:
    #   - unitpro.xef <channelATS ASFCatKey="RIODIOBML2"> paramKPW[372:374]
    #     (the Unity-side channel config words — THE copy the build checks)
    #   - <master>_PrmCfg.bin BFPX container, ST_SECURITY section offset +8
    #   - the SecuritySettings XML attributes in the master DTM dataset
    SEC_BITS = {
        "achillessLevel2": 0, "firmwareUpgrade": 1, "ftp": 2,
        "port502Server": 3, "accessToCPUVia502": 4, "eipServer": 5,
        "tftp": 6, "accessControlList": 7, "snmp": 8, "ntpServer": 9,
        "webServer": 10, "dhcp_bootp": 13,
    }
    KPW_SEC_OFFSET = 372
    # little-endian IPv4 fields in the CPU channel paramKPW
    KPW_IP_OFFSETS = {
        "ip_a": 26, "ip_b": 30, "mask_ab": 34, "gateway": 38, "ip": 42,
        "ip_d": 46, "mask_cd": 50,
    }
    BFPX_PAYLOAD_BASE = 468  # payload origin inside *_PrmCfg.bin

    @staticmethod
    def _u16_decode(data: bytes) -> tuple[str, str]:
        """Decode dataset UTF-16 bytes, remembering whether a BOM was present."""
        if data[:2] == b"\xff\xfe":
            return data[2:].decode("utf-16-le"), "bom"
        return data.decode("utf-16-le"), "nobom"

    @staticmethod
    def _u16_encode(text: str, marker: str) -> bytes:
        raw = text.encode("utf-16-le")
        return (b"\xff\xfe" + raw) if marker == "bom" else raw

    @staticmethod
    def _ip_le_bytes(ip: str) -> list[int]:
        """paramKPW stores IPv4 addresses least-significant octet first
        (192.168.10.1 -> [1, 10, 168, 192])."""
        parts = [int(p) for p in ip.split(".")]
        if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
            raise CEError(f"'{ip}' is not a valid IPv4 address.")
        return parts[::-1]

    @staticmethod
    def _bfpx_section(data: bytes, section: str) -> tuple[int, int]:
        """Locate a section payload in a BFPX *_PrmCfg.bin: returns (abs_offset,
        length). TOC entries are 28 bytes from 0x38: idx u8, name[21], flag u8,
        off u16le, len u16le, type u8."""
        import struct

        pos = 0x38
        while pos + 28 <= len(data):
            name = data[pos + 1: pos + 22].split(b"\x00")[0].decode("latin-1")
            if not name.startswith("ST_"):
                break
            off, ln = struct.unpack_from("<HH", data, pos + 23)
            if name == section:
                return ControlExpertBridge.BFPX_PAYLOAD_BASE + off, ln
            pos += 28
        raise CEError(f"{section} section not found in the DTM PrmCfg container.")

    def _reimport_zef(self, new_zef: str, orig_zef: str, orig_path, fail_hint: str) -> None:
        """Close the live project and reimport new_zef. Closing is destructive,
        so if the modified archive is rejected, roll back by reimporting the
        untouched orig_zef — the caller is NEVER left without an open project —
        then raise CEError(fail_hint.format(error=...)). On success the caller
        sets _project_path as it sees fit."""
        self._do_close(False)
        broker = self._broker()
        self._app = broker.NewApplication()
        try:
            self._app.ImportProject(new_zef)
        except Exception as exc:
            msg = _format_com_error(exc)
            del exc
            try:
                self._app = broker.NewApplication()
                self._app.ImportProject(orig_zef)
                self._project_path = orig_path
                self._needs_saveas = orig_path is not None
            except Exception:
                pass
            raise CEError(fail_hint.format(error=msg)) from None

    def _do_zef_patch(self, patchers: dict) -> dict:
        """Export the project to a full ZEF, run each patcher (entry name ->
        bytes-in/bytes-out callable) on its archive entry, reimport the patched
        archive. The project is reloaded and UNSAVED afterwards."""
        import zipfile

        proj = self._project(write=True)
        orig_path = self._project_path  # restore so save_project(None) still works
        zef = self._temp_path(".zef")
        new_zef = self._temp_path("_mod.zef")
        proj.Export(zef, C.EXPORT_PROJECT_FULL)
        del proj
        try:
            patched = []
            with zipfile.ZipFile(zef) as src, zipfile.ZipFile(
                new_zef, "w", zipfile.ZIP_DEFLATED
            ) as dst:
                names = set(src.namelist())
                missing = [n for n in patchers if n not in names]
                if missing:
                    raise CEError(f"ZEF entries not found: {missing}")
                for item in src.infolist():
                    data = src.read(item.filename)
                    if item.filename in patchers:
                        data = patchers[item.filename](data)
                        patched.append(item.filename)
                    dst.writestr(item, data)
            # ImportProject leaves the in-memory project without a .stu binding;
            # remember the source path so save_project(None) does a SaveAs to it.
            self._reimport_zef(
                new_zef, zef, orig_path,
                "Project reload failed after patching ({error}). The original "
                "project has been restored (unsaved).",
            )
            self._project_path = orig_path
            self._needs_saveas = orig_path is not None
            return {"patched_entries": patched, "reloaded": True}
        finally:
            for p in (zef, new_zef):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def configure_cpu_ethernet(
        self,
        ip: str | None,
        subnet: str | None,
        gateway: str | None,
        ip_a: str | None,
        services: dict[str, bool],
        ip_b: str | None = None,
        ip_d: str | None = None,
    ) -> dict:
        return self._run(self._do_configure_cpu_eth, ip, subnet, gateway, ip_a,
                         services, ip_b, ip_d)

    def _do_configure_cpu_eth(self, ip, subnet, gateway, ip_a, services,
                              ip_b=None, ip_d=None) -> dict:
        """Apply CPU Ethernet/security changes to every copy Unity keeps:
        the application's channel config words (paramKPW) inside unitpro.xef
        (what the build actually validates), the DTM's binary PrmCfg
        ST_SECURITY mask, and the master DTM dataset XML — via a full ZEF
        round-trip. SetControlParameter/UpdateDtmPlcLink cannot do this
        headlessly ('DTM not linked with PLC control' even on GUI-built
        projects), hence the archive surgery."""
        import struct

        _master_name, master_id = self._find_dtm_master()
        changes: list[str] = []
        if services:
            unknown = [k for k in services if k not in self.SEC_BITS]
            if unknown:
                raise CEError(f"Unknown security services: {unknown}. "
                              f"Valid: {sorted(self.SEC_BITS)}")
        if not (ip or subnet or gateway or ip_a or ip_b or ip_d or services):
            return {"changed": [], "note": "nothing to change"}

        def set_attrs_in(segment_re: str, attrs: dict, t: str) -> str:
            m = re.search(segment_re, t, re.S)
            if not m:
                return t
            seg = m.group(0)
            for key, value in attrs.items():
                seg = re.sub(rf'(\b{key}=")[^"]*(")', rf"\g<1>{value}\g<2>", seg)
            return t[: m.start()] + seg + t[m.end():]

        def patch_master_bin(data: bytes) -> bytes:
            text, marker = self._u16_decode(data)
            tcp_attrs: dict[str, str] = {}
            if ip:
                for a in ("staticIPAddress", "bootpIPAddress", "flashIPAddress",
                          "dhcpIPAddress"):
                    tcp_attrs[a] = ip
            if gateway:
                tcp_attrs["gatewayIPAddress"] = gateway
            if subnet:
                tcp_attrs["subNetworkMask"] = subnet
            if ip_a:
                tcp_attrs["staticIPAddressA"] = ip_a
            if ip_b:
                tcp_attrs["staticIPAddressB"] = ip_b
            if ip_d:
                tcp_attrs["staticIPAddressD"] = ip_d
            if tcp_attrs:
                text = set_attrs_in(r"<TcpSettings\b.*?</TcpSettings>", tcp_attrs, text)

            buf_params: dict[str, str] = {}
            if ip:
                buf_params["IPAddressC"] = ip
            if ip_a:
                buf_params["IPAddressA"] = ip_a
            if ip_b:
                buf_params["IPAddressB"] = ip_b
            if ip_d:
                buf_params["IPAddressD"] = ip_d
            if gateway:
                buf_params["GatewayAddress"] = gateway
            if subnet:
                buf_params["SubnetMaskAB"] = subnet
                buf_params["SubnetMaskCD"] = subnet
            for pid, value in buf_params.items():
                text = re.sub(rf'(<Parameter id="{pid}" value=")[^"]*(")',
                              rf"\g<1>{value}\g<2>", text)
            if services:
                sec_attrs = {k: ("true" if v else "false") for k, v in services.items()}
                text = set_attrs_in(r"<SecuritySettings\b[^>]*/>", sec_attrs, text)
                # ACL rows must also permit the service for the EIO subnet, or
                # the build still warns 'TFTP option must be checked' (ACL
                # uses snmpServer where SecuritySettings uses snmp)
                acl_attrs = {("snmpServer" if k == "snmp" else k): v
                             for k, v in sec_attrs.items()
                             if k not in ("achillessLevel2", "accessControlList")}

                def fix_acl(m):
                    seg = m.group(0)
                    for key, value in acl_attrs.items():
                        seg = re.sub(rf'(\b{key}=")[^"]*(")',
                                     rf"\g<1>{value}\g<2>", seg)
                    return seg
                text = re.sub(r"<AclServices\b[^>]*/>", fix_acl, text)
            return self._u16_encode(text, marker)

        def apply_bits(mask: int) -> int:
            for key, value in services.items():
                bit = 1 << self.SEC_BITS[key]
                mask = (mask | bit) if value else (mask & ~bit)
            return mask

        def patch_prmcfg(data: bytes) -> bytes:
            if not services:
                return data
            buf = bytearray(data)
            off, _ln = self._bfpx_section(data, "ST_SECURITY")
            struct.pack_into("<H", buf, off + 8,
                             apply_bits(struct.unpack_from("<H", buf, off + 8)[0]))
            # ACL entries carry their own per-subnet service mask (same bit
            # layout); the build warns about the EIO subnet if its ACL row
            # still denies the newly enabled service. Entries: count u16 at +8,
            # then 16 bytes each: ip[4] mask[4] options u32, flags u16, pad u16.
            off, ln = self._bfpx_section(data, "ST_ACL")
            count = struct.unpack_from("<H", buf, off + 8)[0]
            pos = off + 10
            for _ in range(min(count, max(0, (ln - 10) // 16))):
                struct.pack_into("<H", buf, pos + 12,
                                 apply_bits(struct.unpack_from("<H", buf, pos + 12)[0]))
                pos += 16
            return bytes(buf)

        def patch_xef(data: bytes) -> bytes:
            text = data.decode("utf-8")
            m = re.search(
                r'(<channelATS ASFCatKey="RIODIOBML2"[^>]*>.*?<paramKPW>)'
                r"(.*?)(</paramKPW>)", text, re.S)
            if not m:
                raise CEError("CPU Ethernet channel (RIODIOBML2) not found in "
                              "the application — is this an M580 project?")
            vals = [int(v) for v in re.findall(r'hexaValue="(\d+)"', m.group(2))]
            if services:
                mask = vals[self.KPW_SEC_OFFSET] | (vals[self.KPW_SEC_OFFSET + 1] << 8)
                for key, value in services.items():
                    bit = 1 << self.SEC_BITS[key]
                    mask = (mask | bit) if value else (mask & ~bit)
                vals[self.KPW_SEC_OFFSET] = mask & 0xFF
                vals[self.KPW_SEC_OFFSET + 1] = mask >> 8
                changes.extend(f"{k}={'on' if v else 'off'}" for k, v in services.items())
            for field, value in (("ip", ip), ("ip_a", ip_a), ("ip_b", ip_b),
                                 ("ip_d", ip_d), ("gateway", gateway)):
                if value:
                    o = self.KPW_IP_OFFSETS[field]
                    vals[o: o + 4] = self._ip_le_bytes(value)
                    changes.append(f"{field}={value}")
            if subnet:
                for field in ("mask_ab", "mask_cd"):
                    o = self.KPW_IP_OFFSETS[field]
                    vals[o: o + 4] = self._ip_le_bytes(subnet)
                changes.append(f"subnet={subnet}")
            body = "".join(f'<hexaValue hexaValue="{v}"></hexaValue>' for v in vals)
            text = text[: m.start()] + m.group(1) + body + m.group(3) + text[m.end():]

            # the application's Ethernet port element (ipAddressA=... gateway=...)
            port_attrs: dict[str, str] = {}
            if ip:
                port_attrs["ipAddressC"] = ip
            if ip_a:
                port_attrs["ipAddressA"] = ip_a
            if ip_b:
                port_attrs["ipAddressB"] = ip_b
            if ip_d:
                port_attrs["ipAddressD"] = ip_d
            if gateway:
                port_attrs["gateway"] = gateway
            if subnet:
                port_attrs["subnetMaskAB"] = subnet
                port_attrs["subnetMaskCD"] = subnet
            if port_attrs:
                text = set_attrs_in(
                    r"<\w+ [^>]*\bipAddressA=\"[^>]*\bsubnetMaskAB=\"[^>]*>",
                    port_attrs, text)
            return text.encode("utf-8")

        result = self._do_zef_patch({
            "unitpro.xef": patch_xef,
            f"DTM/BinaryFile/{master_id}.bin": patch_master_bin,
            f"DTM/BinaryFile/{master_id}_PrmCfg.bin": patch_prmcfg,
        })
        return {
            "changed": changes, **result,
            "note": "Project reloaded from the patched archive and UNSAVED — "
                    "run build_project to validate, then save_project.",
        }

    def _find_dtm_master(self) -> tuple[str, str]:
        root = self._dtm_root()
        coll = self._qi(self._wrap(root.Dtms()), "IPServerDtms")
        for dtm in self._iter_dtm_coll(coll):
            alias = dtm.AliasName() if callable(dtm.AliasName) else dtm.AliasName
            dtm_id = dtm.DtmId() if callable(dtm.DtmId) else dtm.DtmId
            return str(alias), str(dtm_id)
        raise CEError("No DTM found in the project (no M580 CPU DTM topology).")

    # ----------------------------------------------------------- simulator

    SIM_EXE = (
        r"C:\Program Files (x86)\Schneider Electric\Control Expert 14.0"
        r"\PLC_Simulator\sim.exe"
    )

    def start_simulator(self, enforce_security: bool) -> dict:
        return self._run(self._do_start_simulator, enforce_security)

    def _do_start_simulator(self, enforce_security: bool) -> dict:
        import subprocess
        import time

        import winreg

        result: dict[str, Any] = {}
        if not enforce_security:
            # 'Use default application (enforce security)' panel option; when
            # set with no password-protected default app, sim.exe blocks on a
            # warning dialog at startup. StaLoad=0 disables it (local test use).
            try:
                key = winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER,
                    r"SOFTWARE\Schneider Electric\PLC-Simulator\ProductVersion0\Settings",
                )
                winreg.SetValueEx(key, "StaLoad", 0, winreg.REG_DWORD, 0)
                winreg.CloseKey(key)
                result["enforce_security"] = False
            except OSError as exc:
                result["registry_warning"] = str(exc)

        already = self._sim_running()
        if already:
            result["simulator"] = "already running"
            return result
        exe = self.SIM_EXE
        if not os.path.isfile(exe):
            raise CEError(f"Simulator not found at {exe}")
        subprocess.Popen([exe])
        for _ in range(30):
            time.sleep(1)
            if self._sim_running():
                result["simulator"] = "started"
                return result
        raise CEError("sim.exe did not come up within 30 s")

    @staticmethod
    def _sim_running() -> bool:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq sim.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
        ).stdout
        return "sim.exe" in out.lower()

    def stop_simulator(self) -> dict:
        return self._run(self._do_stop_simulator)

    def _do_stop_simulator(self) -> dict:
        import subprocess

        if not self._sim_running():
            return {"simulator": "not running"}
        subprocess.run(["taskkill", "/IM", "sim.exe", "/F"], capture_output=True)
        return {"simulator": "stopped"}

    # ----------------------------------------------------------- networks

    def list_networks(self) -> dict:
        return self._run(self._do_list_networks)

    def _do_list_networks(self) -> dict:
        proj = self._project(write=False)
        nets = []
        try:
            for net in _iter_collection(proj.Networks):
                entry = {"name": str(net.Name)}
                for key, attr in (("family", "FamilyName"), ("type", "TypeName"),
                                  ("comment", "Comment")):
                    try:
                        val = getattr(net, attr)
                        if callable(val):
                            val = val()
                        if val:
                            entry[key] = str(val)
                    except Exception:
                        pass
                try:
                    sm = self._get_prop(net, "GetServiceMessaging")
                    if isinstance(sm, tuple) and len(sm) >= 4:
                        entry["ip"] = {
                            "ip_address_conf": sm[0],
                            "ip_address": sm[1],
                            "subnet_mask": sm[2],
                            "gateway": sm[3],
                        }
                except Exception:
                    pass
                nets.append(entry)
        except Exception as exc:
            return {"networks": nets, "error": _format_com_error(exc)}
        return {"networks": nets}

    def add_network(self, name: str, family: str) -> dict:
        return self._run(self._do_add_network, name, family)

    def _do_add_network(self, name: str, family: str) -> dict:
        proj = self._project(write=True)
        try:
            net = self._wrap(self._get_prop(proj.Networks, "Add", name, family))
        except Exception as exc:
            msg = _format_com_error(exc)
            del exc
            raise CEError(
                f"add_network('{name}', '{family}') failed: {msg}. Logical networks exist "
                "on Premium/Quantum platforms (families like 'Ethernet', 'Modbus Plus', "
                "'Fipway'); M340/M580 configure communication on the modules/DTMs instead."
            ) from None
        return {"added": str(net.Name), "family": family}

    def set_network_ip(
        self, name: str, ip_address: str, subnet_mask: str, gateway: str
    ) -> dict:
        return self._run(self._do_set_network_ip, name, ip_address, subnet_mask, gateway)

    def _do_set_network_ip(self, name, ip_address, subnet_mask, gateway) -> dict:
        proj = self._project(write=True)
        net = None
        for cand in _iter_collection(proj.Networks):
            if str(cand.Name).lower() == name.lower():
                net = cand
                break
        if net is None:
            raise CEError(f"Network '{name}' not found (see list_networks).")
        # SetServiceMessaging(IPAddressConf, IPAddress, SubNetwork, Gateway,
        #                     XWayProfile, Network, Station, EthernetConf)
        # IPAddressConf=0 → configured (static) IP; EthernetConf=1 → Ethernet II
        self._get_prop(net, "SetServiceMessaging", 0, ip_address, subnet_mask, gateway, 0, 0, 0, 1)
        return {"network": name, "ip": ip_address, "subnet": subnet_mask, "gateway": gateway}
