"""Complete relative-day travel contract: unit prices, quantities and all transport legs survive persistence."""

from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.schemas.common import WireModel


class TravelRequirements(WireModel):
    attraction_types: list[str] = Field(default_factory=list)
    restaurant_cuisines: list[str] = Field(default_factory=list)
    hotel_features: list[str] = Field(default_factory=list)
    required_names: list[str] = Field(default_factory=list)
    excluded_names: list[str] = Field(default_factory=list)
    allowed_intercity_modes: list[Literal["train", "airplane"]] = Field(default_factory=list)
    required_intercity_modes: list[Literal["train", "airplane"]] = Field(default_factory=list)
    required_innercity_modes: list[Literal["walk", "metro", "taxi"]] = Field(default_factory=list)
    rooms: int | None = Field(default=None, ge=1, le=20)
    room_beds: int | None = Field(default=None, ge=1, le=10)
    max_hotel_cost_per_person_per_night: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    unresolved: list[str] = Field(default_factory=list)


class TravelLeg(WireModel):
    start: str
    end: str
    mode: Literal["walk", "metro", "taxi"]
    start_time: str
    end_time: str
    price: float
    cost: float
    distance: float
    tickets: int | None = None
    cars: int | None = None


class TravelTransferRequest(WireModel):
    mode: Literal["walk", "metro", "taxi"]
    departure: str
    tickets: int | None = None
    cars: int | None = None


class TravelActivity(WireModel):
    type: Literal["airplane", "train", "attraction", "breakfast", "lunch", "dinner", "accommodation"]
    start_time: str
    end_time: str
    price: float
    cost: float
    transports: list[TravelLeg] = Field(default_factory=list)
    position: str | None = None
    start: str | None = None
    end: str | None = None
    flight_id: str | None = Field(default=None, alias="FlightID")
    train_id: str | None = Field(default=None, alias="TrainID")
    tickets: int | None = None
    rooms: int | None = None
    room_type: int | None = None
    transfer_request: TravelTransferRequest | None = None

    def official(self):
        value = self.model_dump(exclude_none=True)
        value.pop("transfer_request", None)  # Tool invocation provenance is not part of the official output shape.
        if "flight_id" in value:
            value["FlightID"] = value.pop("flight_id")
        if "train_id" in value:
            value["TrainID"] = value.pop("train_id")
        return value


class TravelDay(WireModel):
    day: int
    activities: list[TravelActivity]


class TravelHeader(WireModel):
    people_number: int
    start_city: str
    target_city: str


class TravelPlan(TravelHeader):
    itinerary: list[TravelDay]

    def official(self):
        return {
            "people_number": self.people_number,
            "start_city": self.start_city,
            "target_city": self.target_city,
            "itinerary": [
                {"day": day.day, "activities": [a.official() for a in day.activities]} for day in self.itinerary
            ],
        }

    def total_cost(self):
        from decimal import Decimal

        return sum(
            (
                Decimal(str(a.cost)) + sum((Decimal(str(t.cost)) for t in a.transports), Decimal(0))
                for day in self.itinerary
                for a in day.activities
            ),
            Decimal(0),
        )
