"""Backup routes — export/import user data (memories, presets, settings, skills, preferences)."""

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from core.middleware import require_admin
from src.auth_helpers import get_current_user
from src.settings import load_settings, save_settings, load_features, save_features

logger = logging.getLogger(__name__)


def _as_list(value):
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def _as_float(value, default=0.8):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_text(value):
    return str(value).strip() if value not in (None, "") else ""


def _owner_key(owner):
    return _as_text(owner)


def _skill_id(skill):
    return _as_text(skill.get("id") or skill.get("name"))


def _skill_label(skill):
    for key in ("title", "description", "name", "id"):
        value = _as_text(skill.get(key))
        if value:
            return value
    return ""


def setup_backup_routes(memory_manager, preset_manager, skills_manager, research_handler=None) -> APIRouter:
    router = APIRouter(tags=["backup"])

    @router.get("/api/export")
    async def export_data(request: Request):
        """Export all user data as a downloadable JSON file."""
        require_admin(request)
        user = get_current_user(request)

        # Memories (filtered by owner when auth is enabled)
        memories = memory_manager.load(owner=user)

        # Presets (shared across users — export all)
        presets = preset_manager.get_all()

        # Skills (filtered by owner when auth is enabled)
        skills = skills_manager.load(owner=user)

        # Deep Research (stored as files; load into JSON)
        research = []
        research_dir = Path("data/deep_research")
        if research_dir.is_dir():
            for p in research_dir.glob("*.json"):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    # Filter by owner if possible
                    if user and data.get("owner") and data.get("owner") != user:
                        continue
                    research.append({"id": p.stem, "data": data})
                except Exception:
                    continue

        # Settings
        settings = load_settings()

        # Feature flags
        features = load_features()

        # User preferences
        from routes.prefs_routes import _load_for_user
        preferences = _load_for_user(user)

        export_data = {
            "version": 1,
            "exported_at": datetime.now().isoformat(),
            "exported_by": user,
            "memories": memories,
            "presets": presets,
            "skills": skills,
            "research": research,
            "settings": settings,
            "features": features,
            "preferences": preferences,
        }

        filename = f"odysseus_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            content=json.dumps(export_data, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @router.post("/api/import")
    async def import_data(request: Request):
        """Import user data from a previously exported JSON file. Merges with existing data."""
        require_admin(request)
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")

        imported = []

        # ── Memories ──
        if "memories" in body and isinstance(body["memories"], list):
            existing = memory_manager.load_all()
            # Dedup against THIS user's own memories only. Using every tenant's
            # rows (load_all) meant a memory whose text matched any other
            # user's was silently skipped, so the importing user lost their own
            # data. The full store is still saved back below.
            existing_texts = {e.get("text", "").strip().lower()
                              for e in existing if e.get("owner") == user}
            added = 0
            for mem in body["memories"]:
                if not isinstance(mem, dict) or not mem.get("text"):
                    continue
                if mem["text"].strip().lower() in existing_texts:
                    continue  # skip duplicates
                # Assign owner when auth is enabled
                if user and not mem.get("owner"):
                    mem["owner"] = user
                existing.append(mem)
                existing_texts.add(mem["text"].strip().lower())
                added += 1
            memory_manager.save(existing)
            imported.append(f"{added} memories")

        # ── Skills ──
        if "skills" in body and isinstance(body["skills"], list):
            # Map existing for scoped dedup
            existing = skills_manager.load_all()
            # (owner, id) and (owner, title)
            existing_ids = {(_owner_key(s.get("owner")), _skill_id(s)) for s in existing}
            existing_titles = {(_owner_key(s.get("owner")), _as_text(s.get("title", "")).lower()) for s in existing}

            added = 0
            for skill in body["skills"]:
                if not isinstance(skill, dict) or not _skill_label(skill):
                    continue

                target_owner = _owner_key(user or skill.get("owner"))
                sid = _skill_id(skill)
                title = _as_text(skill.get("title") or skill.get("description") or skill.get("name"))

                # Skip if same id or same title already exists for THIS user
                if (target_owner, sid) in existing_ids:
                    continue
                if title and (target_owner, title.lower()) in existing_titles:
                    continue

                # Add via manager (handles disk-backing)
                result = skills_manager.add_skill(
                    title=title,
                    problem=_as_text(skill.get("problem") if skill.get("problem") is not None else skill.get("when_to_use")),
                    solution=_as_text(skill.get("solution") if skill.get("solution") is not None else skill.get("body_extra")),
                    steps=_as_list(skill.get("steps") if skill.get("steps") is not None else skill.get("procedure")),
                    tags=_as_list(skill.get("tags")),
                    source=_as_text(skill.get("source")) or "imported",
                    teacher_model=skill.get("teacher_model"),
                    confidence=_as_float(skill.get("confidence")),
                    owner=user or skill.get("owner"),
                    # New-schema fields
                    name=_as_text(skill.get("name")),
                    description=_as_text(skill.get("description")),
                    category=_as_text(skill.get("category")) or "general",
                    when_to_use=_as_text(skill.get("when_to_use")),
                    procedure=_as_list(skill.get("procedure")),
                    pitfalls=_as_list(skill.get("pitfalls")),
                    verification=_as_list(skill.get("verification")),
                    platforms=_as_list(skill.get("platforms")),
                    requires_toolsets=_as_list(skill.get("requires_toolsets")),
                    fallback_for_toolsets=_as_list(skill.get("fallback_for_toolsets")),
                    status=_as_text(skill.get("status")) or "published",
                    version=_as_text(skill.get("version")) or "1.0.0",
                    body_extra=_as_text(skill.get("body_extra")),
                    created=_as_text(skill.get("created")),
                    uses=int(skill.get("uses", 0)),
                    last_used=skill.get("last_used"),
                )
                if isinstance(result, dict) and not result.get("_deduped"):
                    added += 1
                    # Update local dedup maps
                    existing_ids.add((target_owner, sid))
                    if title:
                        existing_titles.add((target_owner, title.lower()))

            imported.append(f"{added} skills")

        # ── Deep Research ──
        if "research" in body and isinstance(body["research"], list):
            added = 0
            research_dir = Path("data/deep_research")
            research_dir.mkdir(parents=True, exist_ok=True)
            for item in body["research"]:
                if not isinstance(item, dict) or "id" not in item or "data" not in item:
                    continue
                rid = item["id"]
                data = item["data"]
                # Skip if already exists on disk
                target = research_dir / f"{rid}.json"
                if target.exists():
                    continue
                # Assign owner
                if user and not data.get("owner"):
                    data["owner"] = user
                try:
                    with open(target, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    added += 1
                except Exception:
                    continue
            imported.append(f"{added} research runs")

        # ── Presets ──
        if "presets" in body and isinstance(body["presets"], dict):
            current = preset_manager.get_all()
            for key, value in body["presets"].items():
                if isinstance(value, dict):
                    current[key] = value
                elif isinstance(value, list):
                    current[key] = value
            preset_manager.save(current)
            imported.append("presets")

        # ── Settings ──
        if "settings" in body and isinstance(body["settings"], dict):
            current = load_settings()
            current.update(body["settings"])
            save_settings(current)
            imported.append("settings")

        # ── Features ──
        if "features" in body and isinstance(body["features"], dict):
            current = load_features()
            current.update(body["features"])
            save_features(current)
            imported.append("features")

        # ── Preferences ──
        if "preferences" in body and isinstance(body["preferences"], dict):
            from routes.prefs_routes import _load_for_user, _save_for_user
            current = _load_for_user(user)
            current.update(body["preferences"])
            _save_for_user(user, current)
            imported.append("preferences")

        if not imported:
            return {"ok": False, "message": "No recognized data found in the file"}

        return {"ok": True, "imported": imported, "message": f"Imported: {', '.join(imported)}"}

    return router
