from contextlib import asynccontextmanager
import json
import logging
from typing import Annotated, Any
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlmodel import Session, select

from app.database import get_session, init_db
from app.models import (
    AlertCreateRequest,
    AlertExecutionResult,
    AlertSubscription,
    SearchExecuteRequest,
    SearchExecuteResponse,
)
from app.services.filter_engine import DeterministicFilterEngine
from app.services.flight_provider import TravelProviderStrategy, get_travel_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("buscaviajes")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="BuscaViajes - Microservicio de Monitoreo de Vuelos y Alojamientos",
    description="Motor de búsqueda, filtrado determinístico e historial de precios para orquestación con n8n.",
    version="1.0.0",
    lifespan=lifespan,
)

SessionDep = Annotated[Session, Depends(get_session)]
ProviderDep = Annotated[TravelProviderStrategy, Depends(get_travel_provider)]


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    logger.error(
        "Error 422 en %s %s | Body recibido: %s | Query: %s | Errores: %s",
        request.method,
        request.url.path,
        raw_body,
        dict(request.query_params),
        exc.errors(),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors(), "received_body": raw_body},
    )


@app.get("/health", tags=["Health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}


def _extract_payload_dict(raw_data: Any, query_params: dict[str, Any]) -> dict[str, Any]:
    """Desempaqueta payloads provenientes de distintas versiones de nodos Tool de n8n."""
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except json.JSONDecodeError:
            raw_data = {}

    if not isinstance(raw_data, dict):
        raw_data = {}

    # Si n8n envuelve los campos dentro de "body", "parameters", "query" o "input"
    for wrapper_key in ("body", "parameters", "query", "input"):
        inner = raw_data.get(wrapper_key)
        if isinstance(inner, dict) and ("origin" in inner or "chat_id" in inner):
            raw_data = inner
            break
        if isinstance(inner, str):
            try:
                parsed_inner = json.loads(inner)
                if isinstance(parsed_inner, dict):
                    raw_data = parsed_inner
                    break
            except json.JSONDecodeError:
                pass

    if not raw_data and query_params:
        raw_data = dict(query_params)

    return raw_data


@app.post(
    "/alerts/subscribe",
    response_model=AlertSubscription,
    status_code=status.HTTP_201_CREATED,
    tags=["Alerts"],
)
async def subscribe_alert(request: Request, session: SessionDep) -> AlertSubscription:
    """Registra una nueva suscripción de alerta de viaje para un usuario de Telegram."""
    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="replace").strip()
    logger.info("POST /alerts/subscribe recibido | Body=%s | Query=%s", raw_text, dict(request.query_params))

    parsed_json: Any = {}
    if raw_text:
        try:
            parsed_json = json.loads(raw_text)
        except json.JSONDecodeError as err:
            logger.error("JSON malformado recibido desde n8n: %s | Error: %s", raw_text, err)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"JSON inválido recibido: {raw_text}",
            ) from err

    data_dict = _extract_payload_dict(parsed_json, dict(request.query_params))
    try:
        payload = AlertCreateRequest.model_validate(data_dict)
    except ValidationError as exc:
        logger.error("Validación fallida en /alerts/subscribe | Datos=%s | Errores=%s", data_dict, exc.errors())
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc

    if payload.date_to < payload.date_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_to debe ser mayor o igual a date_from.",
        )

    alert = AlertSubscription(
        chat_id=str(payload.chat_id),
        origin=payload.origin.upper().strip(),
        destination=payload.destination.upper().strip(),
        date_from=payload.date_from,
        date_to=payload.date_to,
        max_price=payload.max_price,
        max_stops=payload.max_stops,
        trip_type=payload.trip_type,
        is_active=True,
    )
    session.add(alert)
    session.commit()
    session.refresh(alert)
    logger.info(
        "Alerta creada exitosamente id=%s | %s -> %s (%s a %s) max_price=%s",
        alert.id,
        alert.origin,
        alert.destination,
        alert.date_from,
        alert.date_to,
        alert.max_price,
    )
    return alert


@app.get(
    "/alerts/{chat_id}",
    response_model=list[AlertSubscription],
    tags=["Alerts"],
)
def list_user_alerts(chat_id: str, session: SessionDep) -> list[AlertSubscription]:
    """Lista todos los monitoreos activos asociados a un chat_id de Telegram."""
    stmt = (
        select(AlertSubscription)
        .where(
            AlertSubscription.chat_id == str(chat_id),
            AlertSubscription.is_active.is_(True),
        )
        .order_by(AlertSubscription.created_at.desc())
    )
    return list(session.exec(stmt).all())


@app.delete(
    "/alerts/{alert_id}",
    status_code=status.HTTP_200_OK,
    tags=["Alerts"],
)
def delete_alert(alert_id: int, session: SessionDep) -> dict[str, str | int]:
    """Desactiva (soft-delete) o cancela una alerta activa por su ID."""
    alert = session.get(AlertSubscription, alert_id)
    if not alert or not alert.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alerta {alert_id} no encontrada o ya inactiva.",
        )
    alert.is_active = False
    session.add(alert)
    session.commit()
    return {"status": "cancelled", "alert_id": alert_id}


@app.post(
    "/search/execute",
    response_model=SearchExecuteResponse,
    tags=["Search"],
)
async def execute_search(
    session: SessionDep,
    provider: ProviderDep,
    request: SearchExecuteRequest | None = None,
) -> SearchExecuteResponse:
    """
    Ejecuta la búsqueda contra los proveedores externos (Amadeus / SerpApi),
    aplica el motor de filtrado determinístico (escalas <= max_stops, esperas <= 6h,
    comparación con historial en price_history) y devuelve un JSON ultra-liviano
    con el Top 3 de mejores ofertas por cada alerta.
    """
    alert_id = request.alert_id if request else None
    if alert_id is not None:
        alert = session.get(AlertSubscription, alert_id)
        if not alert or not alert.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Alerta activa con id={alert_id} no encontrada.",
            )
        alerts = [alert]
    else:
        stmt = select(AlertSubscription).where(AlertSubscription.is_active.is_(True))
        alerts = list(session.exec(stmt).all())

    filter_engine = DeterministicFilterEngine(session=session)
    notifications: list[AlertExecutionResult] = []

    for alert in alerts:
        raw_candidates = await provider.search_offers(alert)
        top_3_deals = filter_engine.evaluate_and_rank(alert, raw_candidates)

        if top_3_deals:
            notifications.append(
                AlertExecutionResult(
                    alert_id=alert.id,  # type: ignore[arg-type]
                    chat_id=alert.chat_id,
                    route=f"{alert.origin} -> {alert.destination}",
                    trip_type=alert.trip_type,
                    target_max_price=alert.max_price,
                    top_deals=top_3_deals,
                )
            )

    return SearchExecuteResponse(
        executed_alerts=len(alerts),
        notifications=notifications,
    )
