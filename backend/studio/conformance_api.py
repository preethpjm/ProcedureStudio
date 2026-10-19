"""HTTP endpoints for schemas, BREX, rule sets and stylesheets (the "Schemas and rules" screen)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from .conformance.registry.service import RegistryError


def _err(e: Exception) -> HTTPException:
    return HTTPException(404 if isinstance(e, KeyError) or "no such" in str(e) else 400, str(e).strip("'\""))


def mount_conformance(app: FastAPI, cfg) -> None:
    conf = lambda: cfg.conformance()

    @app.get("/api/conformance")
    def status():
        return conf().status() | {"evaluation_packages": bool(cfg.packages_dir)}

    # ------------------------------------------------------------------ schema packages
    @app.post("/api/conformance/packages/inspect")
    async def inspect(files: list[UploadFile] = File(...), paths: str = Form("[]")):
        """A schema package zip, an issue download zip, or the files of a folder (``paths`` = their
        relative paths, JSON list). Returns what was found and the choices to confirm."""
        rels = json.loads(paths or "[]")
        data = [((rels[i] if i < len(rels) and rels[i] else f.filename), await f.read()) for i, f in enumerate(files)]
        try:
            return conf().inspect_upload(data)
        except (ValueError, RegistryError) as e:
            raise _err(e)

    @app.post("/api/conformance/packages/install")
    def install(body: dict):
        try:
            return conf().install_staged(body["staging"], body["choice"], bool(body.get("replace")))
        except (ValueError, RegistryError, KeyError) as e:
            raise _err(e)

    @app.post("/api/conformance/packages/evaluation")
    def install_evaluation():
        """Install the evaluation subsets shipped with Studio (synthetic; for trying the flow only)."""
        if not cfg.packages_dir:
            raise HTTPException(404, "no evaluation packages are shipped with this installation")
        out = []
        for p in sorted(cfg.packages_dir.iterdir()):
            if p.is_dir():
                try:
                    out.append({"installed": True, **conf().install_package_dir(p)})
                except ValueError as e:
                    out.append({"installed": False, "path": p.name, "error": str(e)})
        return out

    @app.patch("/api/conformance/packages/{key:path}")
    def set_package(key: str, body: dict):
        try:
            conf().registry.set_enabled(key, bool(body.get("enabled", True)))
            return conf().registry.get(key).summary()
        except RegistryError as e:
            raise _err(e)

    @app.delete("/api/conformance/packages/{key:path}")
    def remove_package(key: str):
        try:
            conf().registry.remove(key)
            return {"removed": key}
        except RegistryError as e:
            raise _err(e)

    @app.get("/api/conformance/package-export/{key:path}")
    def export_package(key: str):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                out = conf().registry.export_package(key, Path(tmp) / "package.zip")
            except RegistryError as e:
                raise _err(e)
            name = key.replace("/", "_") + ".zip"
            return Response(out.read_bytes(), media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ BREX
    @app.post("/api/conformance/brex")
    async def add_brex(file: UploadFile = File(...)):
        try:
            return conf().brex.install(await file.read(), file.filename or "")
        except ValueError as e:
            raise _err(e)

    @app.delete("/api/conformance/brex/{dmc}/{issue}")
    def remove_brex(dmc: str, issue: str):
        if not conf().brex.remove(dmc, issue):
            raise HTTPException(404, "no such BREX")
        return {"removed": dmc}

    @app.put("/api/conformance/brex-substitutes")
    def set_substitute(body: dict):
        try:
            conf().brex.set_substitute(body["named"], body["use"])
            return conf().brex.substitutes()
        except (ValueError, KeyError) as e:
            raise _err(e)

    @app.delete("/api/conformance/brex-substitutes/{named}")
    def remove_substitute(named: str):
        conf().brex.remove_substitute(named)
        return conf().brex.substitutes()

    # ------------------------------------------------------------------ rule sets
    @app.post("/api/conformance/rules")
    async def add_rules(file: UploadFile = File(...), name: str = Form(""), applies_to: str = Form("")):
        try:
            return conf().rules.install(await file.read(), file.filename or "", name or None,
                                        json.loads(applies_to) if applies_to else None)
        except ValueError as e:
            raise _err(e)

    @app.patch("/api/conformance/rules/{rid}")
    def change_rules(rid: str, body: dict):
        try:
            return conf().rules.update(rid, body.get("enabled"), body.get("applies_to"), body.get("name"))
        except KeyError as e:
            raise _err(e)

    @app.delete("/api/conformance/rules/{rid}")
    def remove_rules(rid: str):
        try:
            conf().rules.remove(rid)
            return {"removed": rid}
        except KeyError as e:
            raise _err(e)

    @app.get("/api/conformance/rules/{rid}/source")
    def rules_source(rid: str):
        try:
            name, data = conf().rules.source(rid)
        except KeyError as e:
            raise _err(e)
        return Response(data, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'inline; filename="{name}"'})

    # ------------------------------------------------------------------ stylesheets
    @app.post("/api/conformance/stylesheets")
    async def add_stylesheet(file: UploadFile = File(...), kind: str = Form("view"), name: str = Form(""),
                             applies_to: str = Form(""), order: int = Form(100)):
        try:
            return conf().styles.install(await file.read(), file.filename or "", kind, name or None,
                                         json.loads(applies_to) if applies_to else None, order)
        except ValueError as e:
            raise _err(e)

    @app.patch("/api/conformance/stylesheets/{sid}")
    def change_stylesheet(sid: str, body: dict):
        try:
            return conf().styles.update(sid, body.get("enabled"), body.get("applies_to"), body.get("order"), body.get("name"))
        except KeyError as e:
            raise _err(e)

    @app.delete("/api/conformance/stylesheets/{sid}")
    def remove_stylesheet(sid: str):
        try:
            conf().styles.remove(sid)
            return {"removed": sid}
        except (KeyError, ValueError) as e:
            raise _err(e)

    @app.get("/api/conformance/stylesheets/{sid}/source")
    def stylesheet_source(sid: str):
        try:
            name, data = conf().styles.source(sid)
        except KeyError as e:
            raise _err(e)
        mt = "application/zip" if name.endswith(".zip") else "text/plain; charset=utf-8"
        return Response(data, media_type=mt, headers={"Content-Disposition": f'inline; filename="{name}"'})

    # ------------------------------------------------------------------ try any document
    @app.post("/api/conformance/check")
    async def check_any(file: UploadFile = File(...), view: bool = Form(True)):
        """Check any XML document against what is installed (for trying new rules or schemas)."""
        data = await file.read()
        rep = conf().check(data, file.filename or "document.xml")
        wf = next((st for st in rep["stages"] if st["stage"] == 2), None)
        if view and wf and wf["status"] == "passed":
            try:
                rep["view"] = conf().view(data)
            except (ValueError, KeyError) as e:
                rep["view"] = {"error": str(e)}
        return rep
