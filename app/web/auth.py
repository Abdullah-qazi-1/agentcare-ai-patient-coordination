"""Login, registration, and logout for the server-rendered (cookie-session) UI."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.session import get_db
from app.services import patient_service
from app.services.errors import ConflictError, NotFoundError
from app.web.deps import render
from app.web.security import clear_session_cookies, set_session_cookies, verify_csrf

router = APIRouter(tags=["web-auth"])


@router.get("/login")
def login_form(request: Request):
    return render(request, "login.html", active_tab="login")


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        token, _user, _profile = patient_service.authenticate(db, email=email, password=password)
    except NotFoundError:
        return render(
            request,
            "login.html",
            active_tab="login",
            login_error="Invalid email or password.",
            status_code=401,
        )
    response = RedirectResponse(url="/", status_code=303)
    set_session_cookies(response, token=token)
    return response


@router.post("/register")
def register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    phone: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user, _profile = patient_service.register_patient(
            db, name=name, email=email, password=password, phone=phone or None
        )
    except ConflictError as exc:
        return render(
            request,
            "login.html",
            active_tab="register",
            register_error=str(exc),
            status_code=409,
        )
    token = create_access_token(user_id=user.id, role=user.role.value)
    response = RedirectResponse(url="/", status_code=303)
    set_session_cookies(response, token=token)
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookies(response)
    return response
