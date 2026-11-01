import json
import os
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library_extractor import (  # noqa: E402
    FIELDS,
    CredentialsError,
    _run,
    build_record,
    get_family_games,
    get_owned_games,
    merge_libraries,
    read_cache,
    write_cache,
    write_outputs,
)


class MergeLibrariesTests(unittest.TestCase):
    def test_owned_wins_over_family_and_playtime_is_kept(self):
        owned = {
            10: {"name": "Owned Game", "playtime_forever": 120},
        }
        family = {
            10: {"name": "Same Game", "playtime_forever": 0},
            20: {"name": "Shared Game", "playtime_forever": 90},
        }

        merged = merge_libraries(owned, family)

        self.assertEqual(merged[10]["source"], "Owned")
        self.assertEqual(merged[10]["playtime_forever"], 120)
        self.assertEqual(merged[20]["source"], "Family Sharing")
        self.assertEqual(merged[20]["playtime_forever"], 90)

    def test_family_game_without_name_gets_fallback(self):
        merged = merge_libraries({}, {30: {"name": None}})
        self.assertEqual(merged[30]["name"], "Unknown App 30")


class BuildRecordTests(unittest.TestCase):
    def setUp(self):
        self.game = {
            "appid": 620,
            "name": "Portal 2",
            "playtime_forever": 90,
            "source": "Owned",
        }

    def test_ok_record(self):
        entry = {
            "success": True,
            "data": {
                "type": "game",
                "name": "Portal 2",
                "developers": ["Valve"],
                "publishers": ["Valve"],
                "genres": [{"description": "Action"}],
                "categories": [{"description": "Co-op"}],
                "metacritic": {"score": 95},
                "release_date": {"date": "19 Apr, 2011"},
                "is_free": False,
                "price_overview": {"final_formatted": "$9.99"},
                "platforms": {"windows": True, "mac": True, "linux": True},
            },
        }

        record = build_record(620, self.game, entry, "ok")

        self.assertEqual(record["name"], "Portal 2")
        self.assertEqual(record["playtime_hours"], 1.5)
        self.assertEqual(record["developer"], "Valve")
        self.assertEqual(record["genres"], "Action")
        self.assertEqual(record["price"], "$9.99")
        self.assertTrue(record["linux"])
        self.assertEqual(record["metadata_status"], "ok")
        self.assertEqual(set(record), set(FIELDS))

    def test_missing_metadata_is_kept_as_not_found(self):
        record = build_record(620, self.game, {"success": False}, "ok")
        self.assertEqual(record["name"], "Portal 2")
        self.assertEqual(record["metadata_status"], "not_found")
        self.assertEqual(record["genres"], "")

    def test_failed_fetch_is_kept_as_failed(self):
        record = build_record(620, self.game, None, "ok")
        self.assertEqual(record["metadata_status"], "failed")

    def test_non_game_is_skipped(self):
        entry = {"success": True, "data": {"type": "dlc", "name": "DLC"}}
        self.assertIsNone(build_record(620, self.game, entry, "ok"))

    def test_free_game_price(self):
        entry = {
            "success": True,
            "data": {"type": "game", "name": "Free Game", "is_free": True},
        }
        record = build_record(1, {"name": "Free Game"}, entry, "ok")
        self.assertEqual(record["price"], "Free")


class FamilyGamesTests(unittest.TestCase):
    @patch("library_extractor._get_json")
    def test_not_a_member(self, get_json):
        get_json.return_value = {
            "response": {"is_not_member_of_any_group": True}
        }

        games, status = get_family_games(object(), "token")

        self.assertEqual(games, {})
        self.assertEqual(status, "not_member")

    @patch("library_extractor._get_json")
    def test_shared_apps_and_playtime(self, get_json):
        get_json.side_effect = [
            {"response": {"family_groupid": "123"}},
            {
                "response": {
                    "apps": [
                        {"appid": 10, "name": "Shared", "rt_playtime": 30},
                        {"appid": 11, "name": None},
                    ]
                }
            },
        ]

        games, status = get_family_games(object(), "token")

        self.assertEqual(status, "ok")
        self.assertEqual(games[10]["source"], "Family Sharing")
        self.assertEqual(games[10]["playtime_forever"], 30)
        self.assertIsNone(games[11]["name"])


class OwnedGamesFallbackTests(unittest.TestCase):
    @patch("library_extractor._fetch_owned_with")
    def test_falls_back_to_api_key(self, fetch):
        fetch.side_effect = [
            CredentialsError("token rejected"),
            {10: {"name": "Game"}},
        ]

        games = get_owned_games(
            object(), "76561198000000000", api_key="key", access_token="token"
        )

        self.assertEqual(games[10]["name"], "Game")
        self.assertEqual(fetch.call_count, 2)


class CacheTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            entry = {"success": True, "data": {"name": "Game"}}

            write_cache(cache_dir, 10, entry)

            self.assertEqual(read_cache(cache_dir, 10), entry)
            self.assertIsNone(read_cache(cache_dir, 11))


class RunFamilyAuthFailureTests(unittest.TestCase):
    @patch("library_extractor.fetch_appdetails")
    @patch("library_extractor.get_family_games")
    @patch("library_extractor.get_owned_games")
    @patch("library_extractor.mint_web_token")
    @patch("library_extractor.load_auth")
    def test_owned_games_are_exported_and_exit_is_2(
        self,
        load_auth,
        mint_web_token,
        get_owned_games,
        get_family_games,
        fetch_appdetails,
    ):
        load_auth.return_value = {
            "refresh_token": "token",
            "steamid": "76561198000000000",
        }
        mint_web_token.return_value = ("webapi", None, "76561198000000000")
        get_owned_games.return_value = {
            10: {"name": "Owned", "playtime_forever": 60}
        }
        get_family_games.side_effect = CredentialsError("family rejected")
        fetch_appdetails.return_value = (
            {"success": True, "data": {"type": "game", "name": "Owned"}},
            "network",
        )

        args = Namespace(
            auth_file="auth.json",
            login=False,
            no_auth=False,
            login_timeout=1,
            owned_only=False,
            out_dir="",
            cache_dir="",
            no_cache=True,
            delay=0,
            limit=0,
            quiet=True,
            verbose=False,
        )
        manifest = {
            "status": "running",
            "auth_source": None,
            "steam_id": None,
            "family_status": "skipped",
            "counts": {},
            "failed_appids": [],
            "warnings": [],
            "errors": [],
            "outputs": {},
        }

        with tempfile.TemporaryDirectory() as directory:
            args.out_dir = directory
            args.cache_dir = str(Path(directory) / "cache")
            with patch.dict(os.environ, {}, clear=True):
                code = _run(
                    args,
                    manifest,
                    Path(directory),
                    time.monotonic(),
                )

            self.assertEqual(code, 2)
            self.assertEqual(manifest["family_status"], "auth_error")
            self.assertEqual(manifest["status"], "partial")
            self.assertEqual(manifest["counts"]["exported"], 1)
            self.assertTrue(
                (Path(directory) / "steam_library_data.csv").exists()
            )


class ManualTokenOverrideTests(unittest.TestCase):
    @patch("library_extractor.fetch_appdetails")
    @patch("library_extractor.get_family_games")
    @patch("library_extractor.get_owned_games")
    @patch("library_extractor.load_auth")
    def test_manual_token_does_not_read_a_stale_auth_file(
        self,
        load_auth,
        get_owned_games,
        get_family_games,
        fetch_appdetails,
    ):
        load_auth.side_effect = AssertionError("auth.json must not be read")
        get_owned_games.return_value = {
            10: {"name": "Owned", "playtime_forever": 60}
        }
        get_family_games.return_value = ({}, "not_member")
        fetch_appdetails.return_value = (
            {"success": True, "data": {"type": "game", "name": "Owned"}},
            "network",
        )

        args = Namespace(
            auth_file="auth.json",
            login=False,
            no_auth=False,
            login_timeout=1,
            owned_only=False,
            out_dir="",
            cache_dir="",
            no_cache=True,
            delay=0,
            limit=0,
            quiet=True,
            verbose=False,
        )
        manifest = {
            "status": "running",
            "auth_source": None,
            "steam_id": None,
            "family_status": "skipped",
            "counts": {},
            "failed_appids": [],
            "warnings": [],
            "errors": [],
            "outputs": {},
        }

        with tempfile.TemporaryDirectory() as directory:
            args.out_dir = directory
            args.cache_dir = str(Path(directory) / "cache")
            env = {
                "WEBAPI_TOKEN": "manual-token",
                "STEAM_ID": "76561198000000000",
            }
            with patch.dict(os.environ, env, clear=True):
                code = _run(
                    args,
                    manifest,
                    Path(directory),
                    time.monotonic(),
                )

            self.assertEqual(code, 0)
            self.assertEqual(manifest["auth_source"], "WEBAPI_TOKEN")
            self.assertEqual(manifest["counts"]["exported"], 1)


class WriteOutputsTests(unittest.TestCase):
    def test_empty_export_still_writes_json_and_csv_header(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            manifest = {"status": "empty", "counts": {}}

            write_outputs(out_dir, [], manifest)

            self.assertEqual(
                json.loads((out_dir / "steam_library_data.json").read_text()),
                [],
            )
            header = (out_dir / "steam_library_data.csv").read_text().strip()
            self.assertEqual(header, ",".join(FIELDS))
            self.assertTrue((out_dir / "steam_library_manifest.json").exists())

    def test_records_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            record = {field: "" for field in FIELDS}
            record["app_id"] = 10
            record["name"] = "Game"

            write_outputs(out_dir, [record], {"status": "ok"})

            self.assertEqual(
                json.loads((out_dir / "steam_library_data.json").read_text())[0][
                    "name"
                ],
                "Game",
            )
            self.assertIn("Game", (out_dir / "steam_library_data.csv").read_text())


if __name__ == "__main__":
    unittest.main()
