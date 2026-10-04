"""Registration, login, and /auth/me through the real FastAPI app."""


class TestRegister:
    def test_register_returns_a_usable_token(self, client):
        resp = client.post(
            "/auth/register",
            json={"name": "New Patient", "email": "new.patient@example.com", "password": "correct-horse-1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "patient"
        assert body["patient_id"] is not None

        me = client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200
        assert me.json()["email"] == "new.patient@example.com"

    def test_duplicate_email_is_rejected(self, client):
        payload = {"name": "Dup", "email": "dup@example.com", "password": "correct-horse-1"}
        assert client.post("/auth/register", json=payload).status_code == 200
        resp = client.post("/auth/register", json=payload)
        assert resp.status_code == 409


class TestLogin:
    def test_login_success(self, client):
        client.post(
            "/auth/register",
            json={"name": "Login Test", "email": "login@example.com", "password": "correct-horse-1"},
        )
        resp = client.post("/auth/login", json={"email": "login@example.com", "password": "correct-horse-1"})
        assert resp.status_code == 200
        assert resp.json()["access_token"]

    def test_wrong_password_is_401_not_404(self, client):
        client.post(
            "/auth/register",
            json={"name": "Login Test", "email": "login2@example.com", "password": "correct-horse-1"},
        )
        resp = client.post("/auth/login", json={"email": "login2@example.com", "password": "wrong"})
        assert resp.status_code == 401

    def test_unknown_email_is_401(self, client):
        resp = client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever1"})
        assert resp.status_code == 401


class TestLoginRateLimit:
    """Brute-forcing a password by request volume should stop being viable well before
    it succeeds — see app/core/rate_limit.py."""

    def test_repeated_attempts_from_one_client_are_eventually_throttled(self, client):
        from app.api.auth import LOGIN_RATE_LIMIT

        statuses = [
            client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong"}).status_code
            for _ in range(LOGIN_RATE_LIMIT + 1)
        ]

        assert statuses[:LOGIN_RATE_LIMIT] == [401] * LOGIN_RATE_LIMIT
        assert statuses[-1] == 429


class TestMe:
    def test_me_without_token_is_rejected(self, client):
        assert client.get("/auth/me").status_code in (401, 403)
