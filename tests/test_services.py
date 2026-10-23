"""Services: config, exceptions, ApiClient (real HTTP), session, auth."""

import socket

import pytest
import requests
from qgis.core import QgsSettings

from fake_loc_server import CAT_POLE, LOCATION_ID, ORG_ID, PNG_BYTES
from label_on_a_cable.services import config
from label_on_a_cable.services.api_client import ApiClient, _extract_request_id
from label_on_a_cable.services.auth_service import AuthService
from label_on_a_cable.services.exceptions import (
    AuthenticationException, LOCAPIException, NetworkException,
    OTPRequiredException, ServerException, ValidationException,
)
from label_on_a_cable.services.session import (
    clear_session, restore_session, save_session,
)
from label_on_a_cable.models.user import Organization, User


# ----------------------------------------------------------------------
# config
# ----------------------------------------------------------------------

class TestConfig:
    @pytest.fixture(autouse=True)
    def _reset_cache(self, monkeypatch):
        monkeypatch.setattr(config, "_cache", None)
        monkeypatch.delenv("LOC_BASE_URL", raising=False)

    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("LOC_BASE_URL", " https://staging.example.test/ ")
        assert config.get_base_url() == "https://staging.example.test"
        assert config.get_api_base_url() == "https://staging.example.test/api/v1"

    def test_env_file_parsed(self, monkeypatch, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# comment\n\nMALFORMED\n=novalue\n"
            "OTHER=1\nLOC_BASE_URL='https://file.example.test/'\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_ENV_PATH", str(env))
        assert config.get_base_url() == "https://file.example.test"
        assert config._env_values()["OTHER"] == "1"
        assert "" not in config._env_values()

    def test_missing_env_file_uses_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_ENV_PATH", str(tmp_path / "nope.env"))
        assert config.get_base_url() == config.DEFAULT_BASE_URL

    def test_shipped_env_file(self):
        values = config._read_env_file(config._ENV_PATH)
        assert values["LOC_BASE_URL"].startswith("https://")

    def test_cache_read_once(self, monkeypatch, tmp_path):
        env = tmp_path / ".env"
        env.write_text('LOC_BASE_URL="https://a.test"\n', encoding="utf-8")
        monkeypatch.setattr(config, "_ENV_PATH", str(env))
        assert config.get_base_url() == "https://a.test"
        env.write_text("LOC_BASE_URL=https://b.test\n", encoding="utf-8")
        assert config.get_base_url() == "https://a.test"


# ----------------------------------------------------------------------
# exceptions
# ----------------------------------------------------------------------

def test_exception_hierarchy_and_defaults():
    assert issubclass(OTPRequiredException, AuthenticationException)
    for cls in (AuthenticationException, NetworkException,
                ServerException, ValidationException):
        assert issubclass(cls, LOCAPIException)
    assert str(LOCAPIException()) == "An API error occurred"
    assert OTPRequiredException().requires_otp is True
    assert ValidationException().status_code == 400
    assert NetworkException().status_code is None
    s = ServerException("boom", 503, "http://u", "rid", 1.5)
    assert (s.status_code, s.request_url, s.request_id, s.elapsed_seconds) == \
        (503, "http://u", "rid", 1.5)


def test_extract_request_id():
    assert _extract_request_id({"x-trace-id": "T"}) == "T"
    assert _extract_request_id({"x-request-id": "", "trace-id": "Z"}) == "Z"
    assert _extract_request_id({}) == ""


# ----------------------------------------------------------------------
# ApiClient — every endpoint + every status branch over real HTTP
# ----------------------------------------------------------------------

def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestApiClient:
    def test_base_url_from_config_and_explicit(self, loc_server):
        assert ApiClient().base_url == f"{loc_server.base_url}/api/v1"
        assert ApiClient("http://h/api/").base_url == "http://h/api"
        assert ApiClient("http://h")._url("/x/y") == "http://h/x/y"

    def test_token_header(self, api):
        api.set_token("abc")
        assert api.session.headers["Authorization"] == "Bearer abc"
        api.clear_token()
        api.clear_token()  # idempotent
        assert "Authorization" not in api.session.headers

    def test_login_success(self, api, loc_server):
        data = api.login("eng@test.io", "secret")
        assert data["token"] in loc_server.tokens
        req = loc_server.requests_to("login/enabled2fa")[-1]
        assert req["query"] == {"plugin": "true"}
        assert "otp" not in req["body"]

    def test_login_otp_prompt_on_200(self, api):
        with pytest.raises(OTPRequiredException):
            api.login("2fa@test.io", "secret")

    def test_login_with_otp(self, api, loc_server):
        data = api.login("2fa@test.io", "secret", otp="123456")
        assert data["user"]["enabled2fa"] is True
        assert loc_server.requests_to("login/enabled2fa")[-1]["body"]["otp"] == "123456"

    def test_login_wrong_otp(self, api):
        with pytest.raises(AuthenticationException) as ei:
            api.login("2fa@test.io", "secret", otp="000000")
        assert not isinstance(ei.value, OTPRequiredException)
        assert ei.value.status_code == 401

    def test_bad_credentials_401(self, api):
        with pytest.raises(AuthenticationException, match="Invalid email"):
            api.login("eng@test.io", "wrong")

    def test_missing_fields_400(self, api):
        with pytest.raises(ValidationException, match="required"):
            api.login("", "")

    def test_401_with_otp_flag(self, api, loc_server):
        loc_server.fail_next["auth/app/login/enabled2fa"] = (
            401, {"otp": True, "error": "Enter code"}, None)
        with pytest.raises(OTPRequiredException, match="Enter code"):
            api.login("eng@test.io", "secret")

    def test_401_message_fallback(self, authed_api, loc_server):
        loc_server.fail_next["locations"] = (401, {"message": "Token gone"}, None)
        with pytest.raises(AuthenticationException, match="Token gone"):
            authed_api.get_all_locations()

    def test_403_forbidden(self, authed_api, loc_server):
        loc_server.fail_next["locations"] = (403, {}, None)
        with pytest.raises(AuthenticationException, match="Forbidden") as ei:
            authed_api.get_all_locations()
        assert ei.value.status_code == 403

    def test_413_payload_too_large(self, authed_api, loc_server):
        loc_server.fail_next["LOCs/plugin-v3"] = (413, "too big", None)
        with pytest.raises(ValidationException, match="Payload too large") as ei:
            authed_api.push_locs({"location_id": LOCATION_ID})
        assert ei.value.status_code == 413

    def test_404_not_found(self, authed_api):
        with pytest.raises(LOCAPIException, match="Location not found") as ei:
            authed_api.get_location("nope")
        assert ei.value.status_code == 404

    def test_404_default_message(self, authed_api, loc_server):
        loc_server.fail_next["locations/x/logs"] = (404, "", None)
        with pytest.raises(LOCAPIException, match="Resource not found"):
            authed_api.get_location_logs("x")

    def test_5xx_carries_diagnostics(self, authed_api, loc_server):
        loc_server.fail_next["LOCs/plugin-v3"] = (
            500, "Operation timeout", {"X-Request-Id": "req-42"})
        with pytest.raises(ServerException) as ei:
            authed_api.push_locs({"location_id": LOCATION_ID})
        exc = ei.value
        assert exc.status_code == 500
        assert exc.request_id == "req-42"
        assert "Operation timeout" in str(exc)
        assert exc.request_url.endswith("/LOCs/plugin-v3")
        assert exc.elapsed_seconds >= 0

    def test_unexpected_status(self, authed_api, loc_server):
        loc_server.fail_next["locations"] = (418, {}, None)
        with pytest.raises(LOCAPIException, match="Unexpected HTTP 418"):
            authed_api.get_all_locations()

    def test_2xx_non_json_body_is_empty_dict(self, authed_api, loc_server):
        loc_server.fail_next["locations/abc"] = (200, "plain text", None)
        assert authed_api.get_location("abc") == {}

    def test_otp_flag_in_2xx_body(self, authed_api, loc_server):
        loc_server.fail_next["locations"] = (200, {"otp": True}, None)
        with pytest.raises(OTPRequiredException):
            authed_api.get_all_locations()

    def test_connection_errors(self):
        api = ApiClient(f"http://127.0.0.1:{_closed_port()}/api/v1")
        with pytest.raises(NetworkException, match="Connection failed"):
            api.get_all_locations()
        with pytest.raises(NetworkException, match="Connection failed"):
            api.get_locs_for_location(LOCATION_ID)
        with pytest.raises(NetworkException, match="Connection failed"):
            api.health_check()

    def test_timeouts(self, authed_api, loc_server, monkeypatch):
        loc_server.delay["locations"] = 1.0
        with pytest.raises(NetworkException, match="timed out"):
            authed_api._get("locations", timeout=0.2)
        loc_server.delay["LOCs/all-locs-for-location"] = 1.0
        with pytest.raises(NetworkException, match="timed out"):
            authed_api._post("LOCs/all-locs-for-location", json={}, timeout=0.2)
        import label_on_a_cable.services.api_client as mod
        monkeypatch.setattr(mod, "DEFAULT_TIMEOUT", 0.2)
        loc_server.delay["auth/app/login/enabled2fa"] = 1.0
        with pytest.raises(NetworkException, match="timed out"):
            authed_api.health_check()

    def test_push_uses_push_timeout(self, authed_api, monkeypatch):
        import label_on_a_cable.services.api_client as mod
        seen = {}
        real_post = authed_api.session.post

        def spy(url, **kw):
            seen.update(kw)
            return real_post(url, **kw)

        monkeypatch.setattr(authed_api.session, "post", spy)
        authed_api.push_locs({"location_id": LOCATION_ID}, preview=True)
        assert seen["timeout"] == mod.PUSH_TIMEOUT
        assert seen["params"] == {"stats": "true"}

    def test_health_check_any_response(self, api):
        assert api.health_check() is True

    def test_locations_v1(self, authed_api):
        locs = authed_api.get_all_locations()
        assert [l["name"] for l in locs] == ["Testing Location", "Other Location"]

    def test_locations_legacy_fallback(self, authed_api, loc_server):
        loc_server.legacy_locations = True
        locs = authed_api.get_all_locations()
        assert isinstance(locs, list) and locs[0]["Project"]["name"] == "Test Project"
        assert loc_server.requests_to("locations/all-locations-for-user")

    def test_locations_non_404_error_not_swallowed(self, authed_api, loc_server):
        loc_server.fail_next["locations"] = (403, {}, None)
        with pytest.raises(AuthenticationException):
            authed_api.get_all_locations()
        assert not loc_server.requests_to("all-locations-for-user")

    def test_location_endpoints(self, authed_api):
        assert authed_api.get_location(LOCATION_ID)["name"] == "Testing Location"
        assert authed_api.get_location_logs(LOCATION_ID)["logs"]

    def test_category_endpoints(self, authed_api, loc_server):
        assert len(authed_api.get_categories_by_org(ORG_ID)["categories"]) == 4
        assert loc_server.requests[-1]["query"] == {"organization": ORG_ID}
        authed_api.get_categories_by_location(LOCATION_ID)
        assert loc_server.requests[-1]["query"] == {"location": LOCATION_ID}
        assert authed_api.get_category(CAT_POLE)["name"] == "Pole"

    def test_locs_for_location(self, authed_api, loc_server):
        data = authed_api.get_locs_for_location(LOCATION_ID)
        assert data == {"singleLOCs": [], "dualLOCs": [], "multiLoc": []}
        assert loc_server.requests[-1]["body"] == {"location_id": LOCATION_ID}

    def test_unauthenticated_request(self, api):
        with pytest.raises(AuthenticationException):
            api.get_all_locations()


class TestFetchMedia:
    def test_direct_asset(self, authed_api, loc_server):
        assert authed_api.fetch_media(f"{loc_server.base_url}/media/pole.png") == PNG_BYTES
        assert "Authorization" in loc_server.requests[-1]["headers"]

    def test_signed_envelope_with_loopback_rewrite(self, authed_api, loc_server):
        data = authed_api.fetch_media("http://localhost:5000/media/sign/chamber")
        assert data == PNG_BYTES
        paths = [r["path"] for r in loc_server.requests[-2:]]
        assert paths == ["/media/sign/chamber", "/media/chamber.png"]

    def test_off_host_signed_url_drops_auth(self, authed_api, loc_server, media_server):
        loc_server.signed_media_host = media_server.base_url
        data = authed_api.fetch_media(f"{loc_server.base_url}/media/sign/chamber")
        assert data == PNG_BYTES
        off = media_server.requests[-1]
        assert off["path"] == "/media/chamber.png"
        assert "Authorization" not in off["headers"]

    def test_raw_string_envelope(self, authed_api, loc_server):
        assert authed_api.fetch_media(
            f"{loc_server.base_url}/media/sign/raw-string") == PNG_BYTES

    @pytest.mark.parametrize("path", ["/media/sign/empty", "/media/sign/list"])
    def test_envelope_without_url(self, authed_api, loc_server, path):
        with pytest.raises(LOCAPIException, match="no signed URL"):
            authed_api.fetch_media(f"{loc_server.base_url}{path}")

    def test_bad_json_returns_raw_bytes(self, authed_api, loc_server):
        assert authed_api.fetch_media(
            f"{loc_server.base_url}/media/bad-json") == b"{not json"

    def test_http_error(self, authed_api, loc_server):
        with pytest.raises(requests.HTTPError):
            authed_api.fetch_media(f"{loc_server.base_url}/media/missing.png")

    def test_rewrite_loopback(self, api, loc_server):
        assert api._rewrite_loopback("http://127.0.0.1:5000/a?b=1") == \
            f"{loc_server.base_url}/a?b=1"
        assert api._rewrite_loopback("https://cdn.example/a") == "https://cdn.example/a"
        assert api._rewrite_loopback("http://[::1") == "http://[::1"  # ValueError


# ----------------------------------------------------------------------
# session + auth
# ----------------------------------------------------------------------

def _user():
    return User("u1", "Ann", "a@b", "admin", True, Organization("o1", "Org"))


class TestSession:
    def test_round_trip(self):
        save_session("tok", _user())
        token, user = restore_session()
        assert token == "tok"
        assert user.full_name == "Ann" and user.organization.org_id == "o1"
        assert user.enabled_2fa is True

    def test_restore_without_token(self):
        assert restore_session() == (None, None)

    def test_clear(self):
        save_session("tok", _user())
        clear_session()
        assert restore_session() == (None, None)
        assert QgsSettings().value("LOC/user_name") is None


class TestAuthService:
    def test_login_persists_session(self, api, loc_server):
        auth = AuthService(api)
        assert not auth.is_logged_in
        auth.login("eng@test.io", "secret")
        assert auth.is_logged_in
        assert auth.current_user.full_name == "Test Engineer"
        assert auth.current_user.organization.org_id == ORG_ID
        assert api.session.headers["Authorization"] == f"Bearer {auth.token}"
        assert restore_session()[0] == auth.token
        # token is now usable for authenticated endpoints
        assert api.get_all_locations()

    def test_login_failure_leaves_state(self, api):
        auth = AuthService(api)
        with pytest.raises(AuthenticationException):
            auth.login("eng@test.io", "bad")
        assert not auth.is_logged_in and auth.current_user is None

    def test_logout(self, api):
        auth = AuthService(api)
        auth.login("eng@test.io", "secret")
        auth.logout()
        assert not auth.is_logged_in and auth.current_user is None
        assert "Authorization" not in api.session.headers
        assert restore_session() == (None, None)

    def test_try_restore(self, api):
        auth = AuthService(api)
        assert auth.try_restore() is False
        save_session("stored", _user())
        assert AuthService(api).try_restore() is True
        assert api.session.headers["Authorization"] == "Bearer stored"
