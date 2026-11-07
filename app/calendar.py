"""Google Calendar OAuth and appointment validation (requires client credentials)."""
from __future__ import annotations
import os,re,secrets,urllib.parse,httpx
from datetime import datetime,timedelta,timezone
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel,Field
from cryptography.fernet import Fernet,InvalidToken
from app.account import current_user,membership,rpc
from app.cloud import api
from app.booking_hours import OUTSIDE_HOURS, within_booking_hours

router=APIRouter(tags=["calendar"])
REDIRECT="https://callpilot-internal.onrender.com/api/oauth/google/callback"
SCOPES="https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.events.freebusy"
def configured():
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        and os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY"))

def cipher():
    try:return Fernet(os.environ["GOOGLE_TOKEN_ENCRYPTION_KEY"].encode())
    except (KeyError,ValueError):raise HTTPException(503,"Google encryption secret unavailable")

def connection(oid):
    rows=api("calendar_connections",params={"organization_id":"eq."+oid,
        "select":"organization_id,calendar_id,refresh_token_ciphertext,connected_at"})
    return rows[0] if rows else None

@router.get("/api/account/organizations/{oid}/calendar")
def calendar_status(oid:str,user=Depends(current_user)):
    role=membership(oid,user)
    c=connection(oid)
    return {"connected":bool(c),"provider":"google","configured":configured(),
            "connected_at":c["connected_at"] if c else None,"can_manage":role in ("owner","admin")}


@router.delete("/api/account/organizations/{oid}/calendar")
def disconnect_calendar(oid:UUID,user=Depends(current_user)):
    workspace=str(oid)
    membership(workspace,user,("owner","admin"))
    filter_={"organization_id":"eq."+workspace}
    api("oauth_states","DELETE",params=filter_,prefer="return=minimal")
    api("calendar_connections","DELETE",params=filter_,prefer="return=minimal")
    return {"connected":False}

@router.post("/api/account/organizations/{oid}/calendar/connect")
def authorize(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    if not configured():raise HTTPException(503,"Google OAuth client credentials are not configured")
    state=secrets.token_urlsafe(32)
    api("oauth_states","POST",data={"nonce":state,"organization_id":oid,"created_by":user["id"],
       "expires_at":(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()},prefer="return=minimal")
    params={"client_id":os.environ["GOOGLE_OAUTH_CLIENT_ID"],"redirect_uri":REDIRECT,
            "response_type":"code","scope":SCOPES,"state":state,
            "access_type":"offline","prompt":"consent","include_granted_scopes":"false"}
    return {"url":"https://accounts.google.com/o/oauth2/v2/auth?"+urllib.parse.urlencode(params)}

@router.get("/api/oauth/google/callback")
def oauth_callback(code:str|None=None,state:str|None=None,error:str|None=None):
    # Never reflect arbitrary Google error details or authorization codes to the UI.
    if error:
        result="access_denied" if error=="access_denied" else "error"
        return RedirectResponse("/app?calendar="+result+"#appointments")
    if not code or not state:return RedirectResponse("/app?calendar=not_connected#appointments")
    valid=rpc("consume_oauth_state",{"p_nonce":state})
    if not valid:return RedirectResponse("/app?calendar=expired#appointments")
    record=valid[0]
    try:
        with httpx.Client(timeout=15) as c:
            r=c.post("https://oauth2.googleapis.com/token",data={
                "code":code,"client_id":os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                "client_secret":os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
                "redirect_uri":REDIRECT,"grant_type":"authorization_code"})
        r.raise_for_status()
        token=r.json().get("refresh_token")
        if not token: return RedirectResponse("/app?calendar=missing_refresh_token#appointments")
        encrypted=cipher().encrypt(token.encode()).decode()
        api("calendar_connections","POST",params={"on_conflict":"organization_id"},
            data={"organization_id":record["organization_id"],"calendar_id":"primary",
                "refresh_token_ciphertext":encrypted,"connected_by":record["created_by"]},
            prefer="resolution=merge-duplicates,return=minimal")
        return RedirectResponse("/app?calendar=connected#appointments")
    except (httpx.HTTPError,KeyError,ValueError,InvalidToken):
        return RedirectResponse("/app?calendar=error#appointments")

def google_token(oid):
    c=connection(oid)
    if not c:raise HTTPException(409,"Connect Google Calendar first")
    try:refresh=cipher().decrypt(c["refresh_token_ciphertext"].encode()).decode()
    except InvalidToken:raise HTTPException(503,"Calendar token could not be decrypted")
    try:
        with httpx.Client(timeout=12) as client:
            r=client.post("https://oauth2.googleapis.com/token",data={
                "client_id":os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                "client_secret":os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
                "grant_type":"refresh_token","refresh_token":refresh})
        if r.status_code!=200:raise HTTPException(409,"Calendar authorization expired; reconnect")
        return r.json()["access_token"]
    except httpx.RequestError:raise HTTPException(503,"Calendar service unavailable")

def free_busy(access:str,start:datetime,end:datetime):
    try:
        with httpx.Client(timeout=12) as client:
            r=client.post("https://www.googleapis.com/calendar/v3/freeBusy",
                headers={"Authorization":"Bearer "+access},
                json={"timeMin":start.isoformat(),"timeMax":end.isoformat(),
                      "items":[{"id":"primary"}]})
        if r.status_code!=200:raise HTTPException(502,"Could not verify calendar availability")
        try:
            calendars = r.json().get("calendars")
            calendar = calendars.get("primary") if isinstance(calendars, dict) else None
        except (ValueError, AttributeError):
            calendar = None
        if not isinstance(calendar, dict) or calendar.get("errors") or not isinstance(calendar.get("busy"), list):
            raise HTTPException(502, "Calendar availability unavailable")
        return not calendar["busy"]
    except httpx.RequestError:raise HTTPException(503,"Calendar unavailable")

class Slot(BaseModel):
    start:datetime
    duration_minutes:int=Field(default=30,ge=15,le=120)

def check_slot(body:Slot):
    start=body.start
    if not start.tzinfo:raise HTTPException(422,"Appointment time must include timezone")
    end=start+timedelta(minutes=body.duration_minutes)
    if start.astimezone(timezone.utc)<=datetime.now(timezone.utc):
        raise HTTPException(422,"Appointment must be in the future")
    if end>datetime.now(timezone.utc)+timedelta(days=180):
        raise HTTPException(422,"Appointment too far in advance")
    return start,end

@router.post("/api/account/organizations/{oid}/calendar/availability")
def availability(oid:str,body:Slot,user=Depends(current_user)):
    membership(oid,user)
    start,end=check_slot(body)
    if not within_booking_hours(oid, start, end):
        return {"available": False, "reason": OUTSIDE_HOURS, "start": start.isoformat(), "end": end.isoformat()}
    return {"available":free_busy(google_token(oid),start,end),"start":start.isoformat(),"end":end.isoformat()}

class BookSlot(Slot):
    caller_name:str=Field(min_length=1,max_length=100)
    caller_contact:str=Field(min_length=3,max_length=100)
    service:str=Field(min_length=1,max_length=150)


class StaffBooking(BookSlot):
    request_id:UUID


@router.post("/api/account/organizations/{oid}/calendar/book")
def book_from_dashboard(oid:UUID,body:StaffBooking,user=Depends(current_user)):
    from app.voice_actions import confirm_calendar_booking

    workspace=str(oid)
    membership(workspace,user,("owner","admin"))
    start,end=check_slot(body)
    name,contact,service=(body.caller_name.strip(),body.caller_contact.strip(),body.service.strip())
    if len(name)<2 or len(service)<2:
        raise HTTPException(422,"Enter a customer name and an appointment type.")
    if not (re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",contact) or
            (re.fullmatch(r"[+() .0-9-]+",contact) and 7<=len(re.sub(r"\D","",contact))<=15)):
        raise HTTPException(422,"Enter a valid contact email or phone number.")
    if not connection(workspace):
        raise HTTPException(409,"Connect Google Calendar first.")
    try:
        result=confirm_calendar_booking(workspace,"dashboard-"+str(body.request_id),start,end,
                                       body.duration_minutes,name,contact,service)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    if not result["confirmed"]:
        raise HTTPException(409,result["message"])
    return {"confirmed":True,"appointment_id":result["appointment_id"],"start":start.isoformat()}


@router.post("/api/account/organizations/{oid}/calendar/appointments/{aid}/cancel")
def cancel_appointment(oid:UUID,aid:UUID,user=Depends(current_user)):
    workspace=str(oid)
    membership(workspace,user,("owner","admin"))
    filter_={"organization_id":"eq."+workspace,"id":"eq."+str(aid)}
    rows=api("appointment_requests",params={**filter_,"select":"status,calendar_event_id"})
    if not rows:
        raise HTTPException(404,"Appointment not found.")
    row=rows[0]
    if row["status"]=="cancelled":
        return {"status":"cancelled"}
    event_id=row.get("calendar_event_id")
    if row["status"]!="confirmed" or not event_id:
        raise HTTPException(409,"Only confirmed Google Calendar appointments can be cancelled here.")
    headers={"Authorization":"Bearer "+google_token(workspace)}
    url="https://www.googleapis.com/calendar/v3/calendars/primary/events/"+urllib.parse.quote(event_id,safe="")
    try:
        with httpx.Client(timeout=12) as client:
            response=client.get(url,headers=headers)
            if response.status_code in (404,410):
                raise HTTPException(409,"The event could not be verified in the connected calendar.")
            response.raise_for_status()
            event=response.json()
            marker=(event.get("extendedProperties") or {}).get("private") or {}
            if marker.get("callpilot_org")!=workspace or marker.get("callpilot_reservation_id")!=str(aid):
                raise HTTPException(409,"Manage this older appointment directly in Google Calendar.")
            if event.get("status")!="cancelled":
                etag=event.get("etag")
                if not etag:
                    raise HTTPException(502,"Google Calendar did not provide an event version.")
                deleted=client.delete(url,headers={**headers,"If-Match":etag},params={"sendUpdates":"all"})
                if deleted.status_code==412:
                    raise HTTPException(409,"The calendar event changed. Try again.")
                if deleted.status_code!=204:
                    raise HTTPException(502,"Google Calendar did not confirm cancellation.")
    except (httpx.HTTPError,ValueError) as exc:
        raise HTTPException(502,"Google Calendar did not confirm cancellation.") from exc
    api("appointment_requests","PATCH",params={**filter_,"status":"eq.confirmed"},
        data={"status":"cancelled"},prefer="return=minimal")
    return {"status":"cancelled"}


@router.post("/api/account/organizations/{oid}/calendar/book-test")
def book_test(oid:str,body:BookSlot,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    start,end=check_slot(body)
    if not within_booking_hours(oid, start, end):raise HTTPException(409,OUTSIDE_HOURS)
    access=google_token(oid)
    if not free_busy(access,start,end):raise HTTPException(409,"This slot is unavailable")
    event={"summary":"CallPilot test: "+body.service,
           "description":"Created from CallPilot internal dashboard; customer: "+body.caller_name,
           "start":{"dateTime":start.isoformat()},"end":{"dateTime":end.isoformat()}}
    try:
        with httpx.Client(timeout=15) as c:
            r=c.post("https://www.googleapis.com/calendar/v3/calendars/primary/events",
                headers={"Authorization":"Bearer "+access},json=event)
        if r.status_code not in (200,201):raise HTTPException(502,"Calendar did not confirm appointment")
        result=r.json()
    except httpx.RequestError:raise HTTPException(503,"Calendar service unavailable")
    if not result.get("id"):raise HTTPException(502,"Calendar returned no confirmation ID")
    rows=api("appointment_requests","POST",data={
        "organization_id":oid,"caller_name":body.caller_name,"caller_contact":body.caller_contact,
        "service":body.service,"requested_time":start.isoformat(),"status":"confirmed",
        "calendar_event_id":result["id"]},prefer="return=representation")
    return {"confirmed":True,"event_id":result["id"],"appointment_id":rows[0]["id"],
            "start":start.isoformat(),"warning":"Internal test booking, not yet voice-assisted"}
