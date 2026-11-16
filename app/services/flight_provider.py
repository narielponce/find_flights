from abc import ABC, abstractmethod
from datetime import datetime
import logging
import re
from typing import Optional
import httpx

from app.config import settings
from app.models import AlertSubscription, OfferCandidate, TripType

logger = logging.getLogger(__name__)


def parse_iso8601_duration_to_minutes(duration_str: str) -> int:
    """Convierte duraciones ISO-8601 de Amadeus (ej. 'PT14H25M') a minutos."""
    match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?$", duration_str or "")
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return hours * 60 + minutes


class TravelProviderStrategy(ABC):
    """Interfaz base (Strategy Pattern) para proveedores de vuelos y alojamientos."""

    @abstractmethod
    async def search_offers(self, alert: AlertSubscription) -> list[OfferCandidate]:
        pass


class AmadeusProvider(TravelProviderStrategy):
    """Cliente oficial para Amadeus Self-Service API (Flight Offers Search v2)."""

    def __init__(self) -> None:
        self.base_url = settings.amadeus_base_url.rstrip("/")
        self.client_id = settings.amadeus_client_id
        self.client_secret = settings.amadeus_client_secret
        self._access_token: Optional[str] = None

    async def _authenticate(self, client: httpx.AsyncClient) -> str:
        if self._access_token:
            return self._access_token

        resp = await client.post(
            f"{self.base_url}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0,
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        return self._access_token

    @staticmethod
    def _calculate_max_layover_minutes(itineraries: list[dict]) -> int:
        max_layover = 0
        for itinerary in itineraries:
            segments = itinerary.get("segments", [])
            for idx in range(len(segments) - 1):
                arr_raw = segments[idx]["arrival"]["at"]
                dep_raw = segments[idx + 1]["departure"]["at"]
                arr_dt = datetime.fromisoformat(arr_raw)
                dep_dt = datetime.fromisoformat(dep_raw)
                layover = int((dep_dt - arr_dt).total_seconds() // 60)
                if layover > max_layover:
                    max_layover = layover
        return max_layover

    async def search_offers(self, alert: AlertSubscription) -> list[OfferCandidate]:
        if not self.client_id or self.client_id == "your_amadeus_client_id":
            logger.info("Credenciales de Amadeus no configuradas; usando datos de demostración.")
            return self._mock_offers(alert, provider_name="amadeus")

        async with httpx.AsyncClient() as client:
            token = await self._authenticate(client)
            params = {
                "originLocationCode": alert.origin.upper(),
                "destinationLocationCode": alert.destination.upper(),
                "departureDate": alert.date_from.isoformat(),
                "returnDate": alert.date_to.isoformat(),
                "adults": 1,
                "currencyCode": settings.default_currency,
                "max": 15,
            }
            resp = await client.get(
                f"{self.base_url}/v2/shopping/flight-offers",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=20.0,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])

        candidates: list[OfferCandidate] = []
        for item in data:
            itineraries = item.get("itineraries", [])
            stops = max(
                (len(itin.get("segments", [])) - 1 for itin in itineraries),
                default=0,
            )
            max_layover = self._calculate_max_layover_minutes(itineraries)
            total_duration = sum(
                parse_iso8601_duration_to_minutes(itin.get("duration", ""))
                for itin in itineraries
            )
            price_info = item.get("price", {})
            base_price = float(price_info.get("base", price_info.get("total", 0.0)))
            total_price = float(price_info.get("total", 0.0))
            taxes = max(round(total_price - base_price, 2), 0.0)
            carriers = item.get("validatingAirlineCodes", ["N/A"])

            candidates.append(
                OfferCandidate(
                    provider="amadeus",
                    origin=alert.origin.upper(),
                    destination=alert.destination.upper(),
                    departure_date=alert.date_from,
                    return_date=alert.date_to,
                    trip_type=alert.trip_type,
                    airline=carriers[0],
                    stops=stops,
                    max_layover_minutes=max_layover,
                    total_duration_minutes=total_duration,
                    flight_price=base_price,
                    taxes_and_fees=taxes,
                    currency=price_info.get("currency", settings.default_currency),
                    reference_link=(
                        f"https://www.google.com/travel/flights?q=Flights%20to%20"
                        f"{alert.destination.upper()}%20from%20{alert.origin.upper()}%20"
                        f"on%20{alert.date_from.isoformat()}%20through%20{alert.date_to.isoformat()}"
                    ),
                )
            )
        return candidates

    @staticmethod
    def _mock_offers(alert: AlertSubscription, provider_name: str) -> list[OfferCandidate]:
        is_pkg = alert.trip_type == TripType.PACKAGE
        hotel_cost = round(alert.max_price * 0.24, 2) if is_pkg else 0.0
        flight_mult_1 = 0.42 if is_pkg else 0.62
        flight_mult_2 = 0.48 if is_pkg else 0.71
        hotel_name = "Hotel Mar de Canasvieiras 4*" if is_pkg else None
        base_link = (
            f"https://www.google.com/travel/flights?q=Flights+from+{alert.origin}+to+"
            f"{alert.destination}+{alert.date_from.isoformat()}+{alert.date_to.isoformat()}"
        )
        return [
            OfferCandidate(
                provider=provider_name,
                origin=alert.origin.upper(),
                destination=alert.destination.upper(),
                departure_date=alert.date_from,
                return_date=alert.date_to,
                trip_type=alert.trip_type,
                airline="LA (LATAM)",
                stops=1,
                max_layover_minutes=115,
                total_duration_minutes=340,
                flight_price=round(alert.max_price * flight_mult_1, 2),
                hotel_name=hotel_name,
                hotel_price=hotel_cost,
                taxes_and_fees=24.50,
                reference_link=base_link,
            ),
            OfferCandidate(
                provider=provider_name,
                origin=alert.origin.upper(),
                destination=alert.destination.upper(),
                departure_date=alert.date_from,
                return_date=alert.date_to,
                trip_type=alert.trip_type,
                airline="AR (Aerolíneas Argentinas)",
                stops=0,
                max_layover_minutes=0,
                total_duration_minutes=210,
                flight_price=round(alert.max_price * flight_mult_2, 2),
                hotel_name=hotel_name,
                hotel_price=hotel_cost,
                taxes_and_fees=28.00,
                reference_link=base_link,
            ),
            # Oferta con escala absurda (> 6h = 490 min) que el filtro descartará
            OfferCandidate(
                provider=provider_name,
                origin=alert.origin.upper(),
                destination=alert.destination.upper(),
                departure_date=alert.date_from,
                return_date=alert.date_to,
                trip_type=alert.trip_type,
                airline="G3 (GOL)",
                stops=1,
                max_layover_minutes=490,
                total_duration_minutes=710,
                flight_price=round(alert.max_price * 0.35, 2),
                hotel_name=hotel_name,
                hotel_price=hotel_cost,
                taxes_and_fees=20.00,
                reference_link=base_link,
            ),
        ]


class SerpApiProvider(TravelProviderStrategy):
    """Cliente para SerpApi (Google Flights + Google Hotels para paquetes)."""

    SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

    def __init__(self) -> None:
        self.api_key = settings.serpapi_api_key

    async def _fetch_cheapest_hotel(
        self, client: httpx.AsyncClient, alert: AlertSubscription
    ) -> tuple[Optional[str], float]:
        if alert.trip_type != TripType.PACKAGE:
            return None, 0.0

        params = {
            "engine": "google_hotels",
            "q": f"Hotels in {alert.destination.upper()}",
            "check_in_date": alert.date_from.isoformat(),
            "check_out_date": alert.date_to.isoformat(),
            "adults": 1,
            "currency": settings.default_currency,
            "sort_by": 3,  # Precio más bajo
            "api_key": self.api_key,
        }
        resp = await client.get(self.SERPAPI_ENDPOINT, params=params, timeout=20.0)
        resp.raise_for_status()
        properties = resp.json().get("properties", [])
        if not properties:
            return None, 0.0

        best_hotel = properties[0]
        name = best_hotel.get("name", "Hotel Seleccionado")
        total_rate = (
            best_hotel.get("total_rate", {}).get("extracted_lowest")
            or best_hotel.get("rate_per_night", {}).get("extracted_lowest", 0.0)
            * max((alert.date_to - alert.date_from).days, 1)
        )
        return name, float(total_rate or 0.0)

    async def search_offers(self, alert: AlertSubscription) -> list[OfferCandidate]:
        if not self.api_key or self.api_key == "your_serpapi_key":
            logger.info("API Key de SerpApi no configurada; usando datos de demostración.")
            return AmadeusProvider._mock_offers(alert, provider_name="serpapi")

        async with httpx.AsyncClient() as client:
            flight_params = {
                "engine": "google_flights",
                "departure_id": alert.origin.upper(),
                "arrival_id": alert.destination.upper(),
                "outbound_date": alert.date_from.isoformat(),
                "return_date": alert.date_to.isoformat(),
                "currency": settings.default_currency,
                "hl": "es",
                "api_key": self.api_key,
            }
            resp = await client.get(self.SERPAPI_ENDPOINT, params=flight_params, timeout=20.0)
            resp.raise_for_status()
            payload = resp.json()

            hotel_name, hotel_price = await self._fetch_cheapest_hotel(client, alert)

        flights_raw = payload.get("best_flights", []) + payload.get("other_flights", [])
        google_flights_url = payload.get("search_metadata", {}).get(
            "google_flights_url",
            f"https://www.google.com/travel/flights?q={alert.origin}+to+{alert.destination}",
        )

        candidates: list[OfferCandidate] = []
        for option in flights_raw:
            legs = option.get("flights", [])
            layovers = option.get("layovers", [])
            stops = len(layovers)
            max_layover = max((int(lay.get("duration", 0)) for lay in layovers), default=0)
            airline = legs[0].get("airline", "Multiple") if legs else "N/A"
            price = float(option.get("price", 0.0))
            if price <= 0:
                continue

            candidates.append(
                OfferCandidate(
                    provider="serpapi",
                    origin=alert.origin.upper(),
                    destination=alert.destination.upper(),
                    departure_date=alert.date_from,
                    return_date=alert.date_to,
                    trip_type=alert.trip_type,
                    airline=airline,
                    stops=stops,
                    max_layover_minutes=max_layover,
                    total_duration_minutes=int(option.get("total_duration", 0)),
                    flight_price=price,
                    hotel_name=hotel_name,
                    hotel_price=hotel_price,
                    taxes_and_fees=0.0,  # Google Flights ya incluye tasas finales
                    currency=settings.default_currency,
                    reference_link=google_flights_url,
                )
            )
        return candidates


class HybridTravelProvider(TravelProviderStrategy):
    """Combina Amadeus para vuelos y SerpApi (Google Flights + Google Hotels) en paralelo."""

    def __init__(self) -> None:
        self.amadeus = AmadeusProvider()
        self.serpapi = SerpApiProvider()

    async def search_offers(self, alert: AlertSubscription) -> list[OfferCandidate]:
        results: list[OfferCandidate] = []
        if alert.trip_type == TripType.PACKAGE:
            # Para paquetes (vuelo + hotel), priorizamos SerpApi y complementamos
            results.extend(await self.serpapi.search_offers(alert))
        else:
            results.extend(await self.amadeus.search_offers(alert))
            results.extend(await self.serpapi.search_offers(alert))
        return results


def get_travel_provider() -> TravelProviderStrategy:
    provider_key = settings.search_provider.lower().strip()
    if provider_key == "amadeus":
        return AmadeusProvider()
    if provider_key == "serpapi":
        return SerpApiProvider()
    return HybridTravelProvider()

