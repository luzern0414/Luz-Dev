from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_handoff.py"


def valid_handoff() -> dict:
    return {
        "schemaVersion": "1.0",
        "project": {
            "id": "flow-shop",
            "name": "Flow Shop",
            "summary": "A small shop used to validate design handoff consumption.",
            "audience": ["Customers"],
            "outcome": "A customer can complete the core purchase flow.",
            "goals": ["Show a clear product outcome."],
            "nonGoals": ["Do not process real payments in this fixture."],
            "assumptions": [],
            "sources": [],
        },
        "surfaces": ["mini-program"],
        "roles": [
            {
                "id": "customer",
                "name": "Customer",
                "description": "A person buying from the shop.",
            }
        ],
        "designSystem": {
            "positioning": "Clear and calm commerce.",
            "tokenSources": ["design/shared/themes/themes.json"],
            "componentLibrary": "Luz Design component specification.",
            "accessibility": {
                "contrastEvidence": "Contrast reviewed against the approved tokens.",
                "keyboardOrTouchEvidence": "Primary controls use visible focus.",
                "reducedMotion": "Motion is disabled when reduced motion is requested.",
                "contentScaling": "Long content wraps without clipping.",
            },
        },
        "flows": [
            {
                "id": "browse-flow",
                "name": "Browse products",
                "roleId": "customer",
                "entryScreenId": "home-screen",
                "outcome": "The customer reaches a product decision.",
                "steps": ["Open home", "Review products", "Choose a product"],
                "recovery": ["Show retry when the catalog is unavailable."],
            }
        ],
        "screens": [
            {
                "id": "home-screen",
                "surface": "mini-program",
                "title": "Home",
                "route": "pages/home/index",
                "roleIds": ["customer"],
                "flowIds": ["browse-flow"],
                "purpose": "Help the customer choose a product.",
                "states": ["loading", "empty", "error", "offline", "success"],
                "componentIds": ["card-product"],
                "assetIds": ["asset-cover"],
                "apiIntentIds": ["catalog-data"],
                "source": "brief.md",
                "responsive": ["mobile portrait"],
            }
        ],
        "components": [
            {
                "id": "card-product",
                "name": "Product card",
                "surface": "mini-program",
                "variants": ["default"],
                "states": ["default", "focus", "pressed", "disabled", "loading"],
                "contentLimits": "The title uses at most two lines.",
                "acceptance": ["Product facts remain readable at 200 percent zoom."],
            }
        ],
        "assets": [
            {
                "id": "asset-cover",
                "kind": "image",
                "path": "assets/cover.png",
                "purpose": "Illustrate the sample product.",
                "source": "fixture",
                "license": "test-only",
                "textInImage": False,
                "altText": ["Sample product cover"],
            }
        ],
        "apiIntents": [
            {
                "id": "catalog-data",
                "screenId": "home-screen",
                "need": "Read the product catalog.",
                "data": ["Product name", "Price"],
                "actions": [],
                "errorsDisplayed": ["error", "offline", "timeout"],
            }
        ],
        "acceptance": [
            {
                "id": "home-visible",
                "criterion": "The customer can understand the product list.",
                "evidence": "fixture review",
            }
        ],
        "openDecisions": [],
    }

class HandoffValidationTests(unittest.TestCase):
    def write_manifest(self, directory: Path, data: dict) -> Path:
        manifest = directory / "HANDOFF.json"
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return manifest

    def prepare_valid_fixture(self, directory: Path) -> Path:
        (directory / "assets").mkdir(parents=True)
        (directory / "assets" / "cover.png").write_bytes(b"cover")
        return self.write_manifest(directory, valid_handoff())
    def test_accepts_handoff_matching_authoritative_shape(self) -> None:
        from scripts.validate_handoff import validate_handoff

        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.prepare_valid_fixture(Path(temporary))
            self.assertEqual(validate_handoff(valid_handoff(), manifest), [])

    def test_rejects_missing_and_unexpected_top_level_fields(self) -> None:
        from scripts.validate_handoff import validate_handoff

        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.prepare_valid_fixture(Path(temporary))
            missing = valid_handoff()
            del missing["acceptance"]
            self.assertTrue(any("missing required property 'acceptance'" in error for error in validate_handoff(missing, manifest)))

            unexpected = valid_handoff()
            unexpected["sessionId"] = "SES-001"
            self.assertTrue(any("unexpected property 'sessionId'" in error for error in validate_handoff(unexpected, manifest)))

    def test_rejects_invalid_identifiers_and_enums(self) -> None:
        from scripts.validate_handoff import validate_handoff

        mutations = [
            ("project.id", "Bad ID"),
            ("screens.0.surface", "web"),
            ("flows.0.id", ""),
            ("acceptance.0.id", ""),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.prepare_valid_fixture(Path(temporary))
            for path, value in mutations:
                with self.subTest(path=path):
                    data = valid_handoff()
                    target = data
                    parts = path.split(".")
                    for part in parts[:-1]:
                        target = target[int(part)] if part.isdigit() else target[part]
                    target[parts[-1]] = value
                    self.assertTrue(validate_handoff(data, manifest))

    def test_rejects_missing_screen_route_or_asset_path(self) -> None:
        from scripts.validate_handoff import validate_handoff

        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.prepare_valid_fixture(Path(temporary))
            screen_missing = valid_handoff()
            del screen_missing["screens"][0]["route"]
            self.assertTrue(any("screens[0]" in error and "route" in error for error in validate_handoff(screen_missing, manifest)))

            asset_missing = valid_handoff()
            del asset_missing["assets"][0]["path"]
            self.assertTrue(any("assets[0]" in error and "path" in error for error in validate_handoff(asset_missing, manifest)))

    def test_rejects_missing_or_escaping_local_references(self) -> None:
        from scripts.validate_handoff import validate_handoff

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.prepare_valid_fixture(root)
            missing = valid_handoff()
            missing["assets"][0]["path"] = "assets/missing.png"
            self.assertTrue(any("missing" in error for error in validate_handoff(missing, manifest)))

            escaping = valid_handoff()
            escaping["assets"][0]["path"] = "../cover.png"
            (root.parent / "cover.png").write_bytes(b"outside")
            try:
                errors = validate_handoff(escaping, manifest)
                self.assertTrue(any("outside" in error or "escape" in error for error in errors))
            finally:
                (root.parent / "cover.png").unlink(missing_ok=True)

    def test_cli_accepts_valid_and_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.prepare_valid_fixture(Path(temporary))
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            valid = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(VALIDATOR), str(manifest)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)

            manifest.write_text("{broken", encoding="utf-8")
            malformed = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(VALIDATOR), str(manifest)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertNotEqual(malformed.returncode, 0)
            self.assertIn("FAIL", malformed.stdout)


class DesignResolverTests(unittest.TestCase):
    def test_resolver_prefers_cli_then_environment_then_default(self) -> None:
        from scripts.validate_skill_package import resolve_design_root

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cli = base / "cli-design"
            environment = base / "env-design"
            self.assertEqual(
                resolve_design_root(str(cli), {"LUZ_DESIGN_ROOT": str(environment)}, base / "Luz-Dev"),
                cli.resolve(),
            )
            self.assertEqual(
                resolve_design_root(None, {"LUZ_DESIGN_ROOT": str(environment)}, base / "Luz-Dev"),
                environment.resolve(),
            )
            self.assertEqual(
                resolve_design_root(None, {}, base / "Luz-Dev"),
                (base / "Luz-Dev").parent / "Luz-Design",
            )

    def test_design_target_contract_is_skill_plus_theme(self) -> None:
        """Luz Dev depends only on the luz-design skill and a theme file in any known layout."""
        from scripts.validate_skill_package import validate_design_target

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "Luz-Design"
            target.mkdir()
            (target / "SKILL.md").write_text("---\nname: luz-design\n---\n", encoding="utf-8")
            self.assertTrue(any("theme" in error for error in validate_design_target(target)))
            for layout in ("references/themes.json", "design/shared/themes/themes.json"):
                themes = target / layout
                themes.parent.mkdir(parents=True, exist_ok=True)
                themes.write_text('{"shared": {}, "themes": {}}', encoding="utf-8")
                self.assertEqual(validate_design_target(target), [])
                themes.unlink()
            (target / "SKILL.md").write_text("---\nname: other\n---\n", encoding="utf-8")
            self.assertTrue(any("not the luz-design skill" in error for error in validate_design_target(target)))


if __name__ == "__main__":
    unittest.main()
