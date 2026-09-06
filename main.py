import os
import json
import uuid
import atexit
from datetime import datetime, timedelta, date, timezone
from typing import Optional, List, Dict, Any
from enum import Enum
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status, Depends, Header, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, model_validator
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from dotenv import load_dotenv

from database import pool, get_db_connection, init_db

load_dotenv()

# ------------------------------------------------------------------
# Configuration & Security Constants
# ------------------------------------------------------------------
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "admin-secret")
OPS_API_KEY = os.getenv("OPS_API_KEY", "ops-secret")

# Register pool cleanup on interpreter shutdown
atexit.register(pool.close)

# ------------------------------------------------------------------
# Enums & Constants
# ------------------------------------------------------------------
class SeatClass(str, Enum):
    FIRST = "First"
    BUSINESS = "Business"
    ECONOMY = "Economy"

class BookingStatus(str, Enum):
    HELD = "Held"
    CONFIRMED = "Confirmed"
    CANCELLED = "Cancelled"
    REFUNDED = "Refunded"
    REBOOKED = "Rebooked"

class FareType(str, Enum):
    BASIC_ECONOMY = "BasicEconomy"
    FLEXIBLE = "Flexible"
    BUSINESS_FLEX = "BusinessFlex"
    FIRST_FLEX = "FirstFlex"

class UserRole(str, Enum):
    SUPER_ADMIN = "super-admin"
    OPS_AGENT = "ops-agent"

# ------------------------------------------------------------------
# Serialization Helpers
# ------------------------------------------------------------------
def json_serial(obj: Any) -> Any:
    """Serializes datetimes, UUIDs, Enums, and Pydantic models to JSON-compatible types."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "dict"):
        return obj.dict()
    raise TypeError(f"Type {type(obj)} is not JSON serializable")

def to_jsonb(data: Any) -> Optional[Jsonb]:
    """Safely converts Python dictionaries/objects into Postgres Jsonb with date serialization."""
    if data is None:
        return None
    return Jsonb(json.loads(json.dumps(data, default=json_serial)))

def is_expired(expiry_dt: Optional[datetime]) -> bool:
    """Checks whether a given datetime has expired, handling naive and aware datetimes safely."""
    if not expiry_dt:
        return False
    if expiry_dt.tzinfo is not None:
        return expiry_dt < datetime.now(timezone.utc)
    return expiry_dt < datetime.now()

# ------------------------------------------------------------------
# Pydantic Schemas (Request/Response)
# ------------------------------------------------------------------
class SeatAllocationSchema(BaseModel):
    first: int = Field(..., gt=0, description="First class seats")
    business: int = Field(..., gt=0, description="Business class seats")
    economy: int = Field(..., gt=0, description="Economy class seats")

class CreateFlightRequest(BaseModel):
    flight_number: str = Field(..., example="UK101")
    origin: str = Field(..., min_length=3, max_length=3, example="LHR")
    destination: str = Field(..., min_length=3, max_length=3, example="DXB")
    departure_time: datetime
    arrival_time: datetime
    total_capacity: int = Field(..., gt=0, example=100)
    allocations: SeatAllocationSchema
    fare_type: FareType = FareType.BASIC_ECONOMY
    overbooking_buffer: int = Field(0, description="Extra seats allowed over capacity per class, 0 = hard limit")

    @model_validator(mode="after")
    def validate(self):
        total = self.allocations.first + self.allocations.business + self.allocations.economy
        if total != self.total_capacity:
            raise ValueError(f"Seat totals ({total}) must equal total capacity ({self.total_capacity})")
        if self.departure_time >= self.arrival_time:
            raise ValueError("Departure must be before arrival")
        if self.overbooking_buffer < 0:
            raise ValueError("Overbooking buffer cannot be negative")
        return self

class EditFlightRequest(BaseModel):
    origin: Optional[str] = Field(None, min_length=3, max_length=3)
    destination: Optional[str] = Field(None, min_length=3, max_length=3)
    departure_time: Optional[datetime] = None
    arrival_time: Optional[datetime] = None
    allocations: Optional[SeatAllocationSchema] = None
    fare_type: Optional[FareType] = None
    overbooking_buffer: Optional[int] = None

class FlightResponse(BaseModel):
    id: int
    flight_number: str
    origin: str
    destination: str
    departure_time: datetime
    arrival_time: datetime
    total_capacity: int
    fare_type: FareType
    overbooking_buffer: int
    seat_classes: List[Dict[str, Any]]

class SearchResult(BaseModel):
    flight_id: int
    flight_number: str
    departure_time: datetime
    arrival_time: datetime
    class_name: str
    available_seats: int
    fare_type: FareType
    price: Optional[float] = None

class SeatHoldRequest(BaseModel):
    flight_id: int
    seat_class: SeatClass
    passenger_name: str = Field(..., min_length=1)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    idempotency_key: Optional[str] = None

class ConfirmBookingRequest(BaseModel):
    booking_id: int

class CancelBookingRequest(BaseModel):
    booking_id: int
    reason: Optional[str] = None

class WaitlistRequest(BaseModel):
    flight_id: int
    seat_class: SeatClass
    passenger_name: str
    email: str
    loyalty_tier: Optional[int] = Field(0, ge=0, description="Higher = more priority")
    fare_type: FareType = FareType.BASIC_ECONOMY

class WaitlistPromoteRequest(BaseModel):
    flight_id: int
    seat_class: SeatClass
    max_promotions: int = Field(1, gt=0)

# ------------------------------------------------------------------
# Security: Admin Role & Idempotency
# ------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
idempotency_header = APIKeyHeader(name="Idempotency-Key", auto_error=False)

def get_current_user_role(api_key: Optional[str] = Depends(api_key_header)) -> UserRole:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    if api_key == ADMIN_API_KEY:
        return UserRole.SUPER_ADMIN
    if api_key == OPS_API_KEY:
        return UserRole.OPS_AGENT
    raise HTTPException(status_code=403, detail="Invalid API Key")

def require_super_admin(role: UserRole = Depends(get_current_user_role)) -> UserRole:
    if role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    return role

def require_ops_or_super(role: UserRole = Depends(get_current_user_role)) -> UserRole:
    if role not in [UserRole.SUPER_ADMIN, UserRole.OPS_AGENT]:
        raise HTTPException(status_code=403, detail="Ops or admin access required")
    return role

async def check_idempotency(request: Request, key: Optional[str] = Depends(idempotency_header)) -> Optional[str]:
    if not key:
        return None
    method_path = f"{request.method}_{request.url.path}"
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT response_data FROM idempotency_keys WHERE key = %s AND method_path = %s",
                (key, method_path)
            )
            row = cur.fetchone()
            if row:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"message": "Duplicate request", "cached_response": row["response_data"]}
                )
    return key

def store_idempotency(key: str, method_path: str, response_data: dict, conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO idempotency_keys (key, method_path, response_data)
            VALUES (%s, %s, %s)
            ON CONFLICT (key, method_path) DO NOTHING
            """,
            (key, method_path, to_jsonb(response_data))
        )

# ------------------------------------------------------------------
# Database Helper Functions
# ------------------------------------------------------------------
def get_audit_user(role: UserRole) -> str:
    return f"role:{role.value}"

def audit_log(conn, table_name: str, record_id: Optional[int], action: str, changed_by: str, old_data: Any = None, new_data: Any = None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (table_name, record_id, action, changed_by, old_data, new_data)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (table_name, record_id, action, changed_by, to_jsonb(old_data), to_jsonb(new_data))
        )

def get_flight_response(flight_id: int, conn) -> FlightResponse:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM flights WHERE id = %s", (flight_id,))
        flight = cur.fetchone()
        if not flight:
            raise HTTPException(404, "Flight not found")
        cur.execute(
            "SELECT class_name, total_allocated, booked_count FROM flight_seat_classes WHERE flight_id = %s ORDER BY id ASC",
            (flight_id,)
        )
        classes = cur.fetchall()
        fare_val = flight.get("fare_type") or "BasicEconomy"
        fare_enum = FareType(fare_val) if fare_val in FareType._value2member_map_ else FareType.BASIC_ECONOMY
        return FlightResponse(
            id=flight["id"],
            flight_number=flight["flight_number"],
            origin=flight["origin"],
            destination=flight["destination"],
            departure_time=flight["departure_time"],
            arrival_time=flight["arrival_time"],
            total_capacity=flight["total_capacity"],
            fare_type=fare_enum,
            overbooking_buffer=flight.get("overbooking_buffer") or 0,
            seat_classes=classes
        )

# ------------------------------------------------------------------
# Email Service Placeholder
# ------------------------------------------------------------------
def send_email(to: str, subject: str, body: str = ""):
    try:
        print(f"[EMAIL] To: {to} | Subject: {subject} | Body: {body}")
    except Exception:
        pass

# ------------------------------------------------------------------
# FastAPI App Lifecycle & Setup
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="Flight Management System",
    description="Full-featured airline flight inventory, atomic seat locking, waitlist, and audit engine.",
    version="2.0.0",
    lifespan=lifespan
)

# -------------------- Health & DB Check --------------------
@app.get("/")
def home():
    return {"message": "Flight Management API is running!", "version": "2.0.0"}

@app.get("/db-check")
def db_check():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM flights;")
                count = cur.fetchone()[0]
        return {"status": "Connected successfully", "flights_count": count}
    except Exception as e:
        raise HTTPException(500, detail=str(e))

# -------------------- Admin: Create Flight --------------------
@app.post("/flights", status_code=status.HTTP_201_CREATED, response_model=FlightResponse)
def create_flight(
    payload: CreateFlightRequest,
    role: UserRole = Depends(require_super_admin),
    idem_key: Optional[str] = Depends(check_idempotency)
):
    method_path = "POST_/flights"
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # Check duplicate flight number for same day/route
                cur.execute(
                    """
                    SELECT id FROM flights 
                    WHERE flight_number = %s AND DATE(departure_time) = DATE(%s) AND origin = %s AND destination = %s
                    """,
                    (payload.flight_number, payload.departure_time, payload.origin, payload.destination)
                )
                if cur.fetchone():
                    raise HTTPException(400, "Duplicate flight number for same day/route")

                # Insert flight
                cur.execute(
                    """
                    INSERT INTO flights (flight_number, origin, destination, departure_time, arrival_time,
                                         total_capacity, fare_type, overbooking_buffer, cancelled)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                    RETURNING id
                    """,
                    (payload.flight_number, payload.origin, payload.destination, payload.departure_time,
                     payload.arrival_time, payload.total_capacity, payload.fare_type.value, payload.overbooking_buffer)
                )
                flight_id = cur.fetchone()["id"]

                # Insert seat classes
                for cls_name, count in [
                    (SeatClass.FIRST, payload.allocations.first),
                    (SeatClass.BUSINESS, payload.allocations.business),
                    (SeatClass.ECONOMY, payload.allocations.economy)
                ]:
                    cur.execute(
                        """
                        INSERT INTO flight_seat_classes (flight_id, class_name, total_allocated, booked_count, overbooking_buffer)
                        VALUES (%s, %s, %s, 0, %s)
                        """,
                        (flight_id, cls_name.value, count, payload.overbooking_buffer)
                    )

                # Audit log
                audit_log(conn, "flights", flight_id, "CREATE", changed_by=get_audit_user(role),
                          new_data=payload.model_dump(mode="json"))

                # Store idempotency
                if idem_key:
                    response = {"id": flight_id, "status": "created"}
                    store_idempotency(idem_key, method_path, response, conn)

                conn.commit()
                return get_flight_response(flight_id, conn)
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Admin: Edit Flight --------------------
@app.put("/flights/{flight_id}", response_model=FlightResponse)
def edit_flight(
    flight_id: int,
    payload: EditFlightRequest,
    role: UserRole = Depends(require_super_admin),
    idem_key: Optional[str] = Depends(check_idempotency)
):
    method_path = f"PUT_/flights/{flight_id}"
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # Lock flight record
                cur.execute("SELECT * FROM flights WHERE id = %s FOR UPDATE", (flight_id,))
                current_flight = cur.fetchone()
                if not current_flight:
                    raise HTTPException(404, "Flight not found")

                update_fields = []
                params = []
                old_data = dict(current_flight)

                if payload.origin is not None:
                    update_fields.append("origin = %s")
                    params.append(payload.origin)
                if payload.destination is not None:
                    update_fields.append("destination = %s")
                    params.append(payload.destination)
                if payload.departure_time is not None:
                    update_fields.append("departure_time = %s")
                    params.append(payload.departure_time)
                if payload.arrival_time is not None:
                    update_fields.append("arrival_time = %s")
                    params.append(payload.arrival_time)
                if payload.fare_type is not None:
                    update_fields.append("fare_type = %s")
                    params.append(payload.fare_type.value)
                if payload.overbooking_buffer is not None:
                    update_fields.append("overbooking_buffer = %s")
                    params.append(payload.overbooking_buffer)

                if update_fields:
                    params.append(flight_id)
                    cur.execute(
                        f"UPDATE flights SET {', '.join(update_fields)} WHERE id = %s RETURNING *",
                        params
                    )
                    updated_flight = dict(cur.fetchone())
                else:
                    updated_flight = dict(current_flight)

                # Handle seat allocations updates if provided
                if payload.allocations:
                    cur.execute("SELECT class_name, booked_count FROM flight_seat_classes WHERE flight_id = %s", (flight_id,))
                    booked = {row["class_name"]: row["booked_count"] for row in cur.fetchall()}
                    for cls_name, new_count in [
                        (SeatClass.FIRST, payload.allocations.first),
                        (SeatClass.BUSINESS, payload.allocations.business),
                        (SeatClass.ECONOMY, payload.allocations.economy)
                    ]:
                        if new_count < booked.get(cls_name.value, 0):
                            raise HTTPException(400, f"Cannot shrink {cls_name.value} below already-booked count ({booked.get(cls_name.value, 0)})")
                        cur.execute(
                            "UPDATE flight_seat_classes SET total_allocated = %s WHERE flight_id = %s AND class_name = %s",
                            (new_count, flight_id, cls_name.value)
                        )
                    # Update total capacity to match allocations
                    new_total = payload.allocations.first + payload.allocations.business + payload.allocations.economy
                    cur.execute("UPDATE flights SET total_capacity = %s WHERE id = %s", (new_total, flight_id))
                    updated_flight["total_capacity"] = new_total

                if payload.overbooking_buffer is not None:
                    cur.execute(
                        "UPDATE flight_seat_classes SET overbooking_buffer = %s WHERE flight_id = %s",
                        (payload.overbooking_buffer, flight_id)
                    )

                # Audit log
                audit_log(conn, "flights", flight_id, "EDIT", changed_by=get_audit_user(role),
                          old_data=old_data, new_data=updated_flight)

                if idem_key:
                    response = {"id": flight_id, "status": "updated"}
                    store_idempotency(idem_key, method_path, response, conn)

                conn.commit()
                return get_flight_response(flight_id, conn)
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Admin: Cancel Flight (with refund) --------------------
@app.post("/flights/{flight_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_flight(
    flight_id: int,
    role: UserRole = Depends(require_super_admin)
):
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # Fetch and lock flight
                cur.execute("SELECT * FROM flights WHERE id = %s FOR UPDATE", (flight_id,))
                flight = cur.fetchone()
                if not flight:
                    raise HTTPException(404, "Flight not found")

                # Fetch all confirmed bookings for this flight
                cur.execute(
                    """
                    SELECT id, passenger_name, email, seat_class, fare_type
                    FROM bookings
                    WHERE flight_id = %s AND status = 'Confirmed'
                    FOR UPDATE
                    """,
                    (flight_id,)
                )
                bookings = cur.fetchall()

                # Process cancellations and refunds based on fare type
                for b in bookings:
                    fare_str = b.get("fare_type") or "BasicEconomy"
                    fare = FareType(fare_str) if fare_str in FareType._value2member_map_ else FareType.BASIC_ECONOMY
                    new_status = "Refunded" if fare in (FareType.FLEXIBLE, FareType.BUSINESS_FLEX, FareType.FIRST_FLEX) else "Cancelled"

                    cur.execute("UPDATE bookings SET status = %s WHERE id = %s", (new_status, b["id"]))
                    cur.execute(
                        "UPDATE flight_seat_classes SET booked_count = GREATEST(0, booked_count - 1) WHERE flight_id = %s AND class_name = %s",
                        (flight_id, b["seat_class"])
                    )
                    audit_log(conn, "bookings", b["id"], "CANCEL_FLIGHT", changed_by=get_audit_user(role),
                              old_data={"status": "Confirmed"}, new_data={"status": new_status})

                    if b.get("email"):
                        send_email(b["email"], f"Flight {flight['flight_number']} cancelled. Booking updated to {new_status}.")

                # Mark flight as cancelled
                cur.execute("UPDATE flights SET cancelled = TRUE WHERE id = %s", (flight_id,))
                audit_log(conn, "flights", flight_id, "CANCEL_FLIGHT", changed_by=get_audit_user(role),
                          old_data={"cancelled": flight.get("cancelled", False)}, new_data={"cancelled": True})

                conn.commit()
                return {"message": f"Flight {flight_id} cancelled. {len(bookings)} bookings processed."}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Search Flights --------------------
@app.get("/search", response_model=List[SearchResult])
def search_flights(origin: str, destination: str, departure_date: date):
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT 
                        f.id as flight_id, f.flight_number, f.departure_time, f.arrival_time,
                        sc.class_name, 
                        (sc.total_allocated + COALESCE(sc.overbooking_buffer, f.overbooking_buffer, 0) - sc.booked_count) AS available_seats,
                        f.fare_type
                    FROM flights f
                    JOIN flight_seat_classes sc ON f.id = sc.flight_id
                    WHERE UPPER(f.origin) = UPPER(%s) AND UPPER(f.destination) = UPPER(%s) 
                      AND DATE(f.departure_time) = %s
                      AND (f.cancelled IS FALSE OR f.cancelled IS NULL)
                      AND (sc.total_allocated + COALESCE(sc.overbooking_buffer, f.overbooking_buffer, 0) - sc.booked_count) > 0
                    ORDER BY f.departure_time ASC, sc.class_name ASC
                    """,
                    (origin.strip(), destination.strip(), departure_date)
                )
                results = cur.fetchall()

                formatted_results = []
                for row in results:
                    fare_str = row.get("fare_type") or "BasicEconomy"
                    fare_type = FareType(fare_str) if fare_str in FareType._value2member_map_ else FareType.BASIC_ECONOMY

                    price = 100.0
                    if row["class_name"] == "Business":
                        price = 250.0
                    elif row["class_name"] == "First":
                        price = 500.0

                    formatted_results.append(SearchResult(
                        flight_id=row["flight_id"],
                        flight_number=row["flight_number"],
                        departure_time=row["departure_time"],
                        arrival_time=row["arrival_time"],
                        class_name=row["class_name"],
                        available_seats=row["available_seats"],
                        fare_type=fare_type,
                        price=price
                    ))
                return formatted_results
        except Exception as e:
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Seat Hold (Atomic) --------------------
@app.post("/bookings/hold", status_code=status.HTTP_201_CREATED)
def hold_seat(
    payload: SeatHoldRequest,
    role: UserRole = Depends(require_ops_or_super),
    idem_key: Optional[str] = Depends(check_idempotency)
):
    method_path = "POST_/bookings/hold"
    effective_idem_key = idem_key or payload.idempotency_key

    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # 1. Lock seat class row
                cur.execute(
                    """
                    SELECT sc.id, sc.flight_id, sc.class_name, sc.total_allocated, sc.booked_count,
                           COALESCE(sc.overbooking_buffer, f.overbooking_buffer, 0) as overbooking_buffer,
                           f.fare_type as flight_fare_type, f.cancelled
                    FROM flight_seat_classes sc
                    JOIN flights f ON f.id = sc.flight_id
                    WHERE sc.flight_id = %s AND sc.class_name = %s
                    FOR UPDATE OF sc
                    """,
                    (payload.flight_id, payload.seat_class.value)
                )
                seat_class = cur.fetchone()
                if not seat_class:
                    conn.rollback()
                    raise HTTPException(400, "Seat class unavailable or does not exist")

                if seat_class.get("cancelled"):
                    conn.rollback()
                    raise HTTPException(400, "Flight is cancelled")

                # 2. Check seat availability including overbooking buffer
                available = seat_class["total_allocated"] + seat_class["overbooking_buffer"] - seat_class["booked_count"]
                if available <= 0:
                    conn.rollback()
                    raise HTTPException(400, "No seats available in this class")

                # 3. Increment booked_count atomically
                cur.execute(
                    "UPDATE flight_seat_classes SET booked_count = booked_count + 1 WHERE id = %s RETURNING booked_count",
                    (seat_class["id"],)
                )

                # 4. Insert booking with status HELD and 15 min expiration
                hold_expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
                fare_type = seat_class.get("flight_fare_type") or "BasicEconomy"
                cur.execute(
                    """
                    INSERT INTO bookings (flight_id, seat_class, passenger_name, email, status, hold_expires_at, fare_type)
                    VALUES (%s, %s, %s, %s, 'Held', %s, %s)
                    RETURNING id
                    """,
                    (payload.flight_id, payload.seat_class.value, payload.passenger_name,
                     payload.email, hold_expiry, fare_type)
                )
                booking_id = cur.fetchone()["id"]

                # 5. Audit log
                audit_log(conn, "bookings", booking_id, "HOLD", changed_by=get_audit_user(role),
                          new_data={"status": "Held", "expires": hold_expiry.isoformat()})

                response = {
                    "booking_id": booking_id,
                    "hold_expires_at": hold_expiry.isoformat(),
                    "status": "Held"
                }

                if effective_idem_key:
                    store_idempotency(effective_idem_key, method_path, response, conn)

                conn.commit()
                return response
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Confirm Booking --------------------
@app.post("/bookings/confirm", status_code=status.HTTP_200_OK)
def confirm_booking(
    payload: ConfirmBookingRequest,
    role: UserRole = Depends(require_ops_or_super)
):
    booking_id = payload.booking_id
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM bookings WHERE id = %s FOR UPDATE", (booking_id,))
                booking = cur.fetchone()
                if not booking:
                    raise HTTPException(404, "Booking not found")

                if booking["status"] == "Confirmed":
                    return {"message": "Already confirmed", "booking_id": booking_id}
                if booking["status"] != "Held":
                    raise HTTPException(400, f"Cannot confirm booking with status {booking['status']}")

                # Check if hold expired
                if is_expired(booking.get("hold_expires_at")):
                    # Release seat
                    cur.execute(
                        """
                        UPDATE flight_seat_classes 
                        SET booked_count = GREATEST(0, booked_count - 1)
                        WHERE flight_id = %s AND class_name = %s
                        """,
                        (booking["flight_id"], booking["seat_class"])
                    )
                    cur.execute("UPDATE bookings SET status = 'Cancelled' WHERE id = %s", (booking_id,))
                    audit_log(conn, "bookings", booking_id, "HOLD_EXPIRED", changed_by=get_audit_user(role),
                              old_data={"status": "Held"}, new_data={"status": "Cancelled"})
                    conn.commit()
                    raise HTTPException(400, "Hold expired – seat released back to inventory")

                # Confirm booking
                cur.execute("UPDATE bookings SET status = 'Confirmed' WHERE id = %s RETURNING *", (booking_id,))
                audit_log(conn, "bookings", booking_id, "CONFIRM", changed_by=get_audit_user(role),
                          old_data={"status": "Held"}, new_data={"status": "Confirmed"})

                if booking.get("email"):
                    send_email(booking["email"], f"Booking {booking_id} confirmed!")

                conn.commit()
                return {"message": "Booking confirmed", "booking_id": booking_id}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# Backwards compatibility path param route
@app.post("/bookings/{booking_id}/confirm", status_code=status.HTTP_200_OK)
def confirm_booking_by_path(
    booking_id: int,
    role: UserRole = Depends(require_ops_or_super)
):
    return confirm_booking(ConfirmBookingRequest(booking_id=booking_id), role=role)

# -------------------- Cancel Booking (with refund) --------------------
@app.post("/bookings/cancel", status_code=status.HTTP_200_OK)
def cancel_booking(
    payload: CancelBookingRequest,
    role: UserRole = Depends(require_ops_or_super)
):
    booking_id = payload.booking_id
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM bookings WHERE id = %s FOR UPDATE", (booking_id,))
                booking = cur.fetchone()
                if not booking:
                    raise HTTPException(404, "Booking not found")
                if booking["status"] in ("Cancelled", "Refunded"):
                    return {"message": f"Booking already {booking['status']}", "booking_id": booking_id}

                # Determine refund based on fare type
                fare_str = booking.get("fare_type") or "BasicEconomy"
                fare = FareType(fare_str) if fare_str in FareType._value2member_map_ else FareType.BASIC_ECONOMY

                refund_amount = 0.0
                if fare in (FareType.FLEXIBLE, FareType.BUSINESS_FLEX, FareType.FIRST_FLEX):
                    refund_amount = 100.0  # full refund placeholder
                elif fare == FareType.BASIC_ECONOMY:
                    refund_amount = 0.0  # non-refundable

                new_status = "Refunded" if refund_amount > 0 else "Cancelled"
                cur.execute("UPDATE bookings SET status = %s WHERE id = %s", (new_status, booking_id))

                # Release seat if booking was actively held or confirmed
                if booking["status"] in ("Confirmed", "Held"):
                    cur.execute(
                        "UPDATE flight_seat_classes SET booked_count = GREATEST(0, booked_count - 1) WHERE flight_id = %s AND class_name = %s",
                        (booking["flight_id"], booking["seat_class"])
                    )

                audit_log(conn, "bookings", booking_id, "CANCEL", changed_by=get_audit_user(role),
                          old_data={"status": booking["status"]},
                          new_data={"status": new_status, "refund_amount": refund_amount, "reason": payload.reason})

                if booking.get("email"):
                    send_email(booking["email"], f"Booking {booking_id} cancelled. Refund amount: ${refund_amount:.2f}")

                conn.commit()
                return {
                    "message": f"Booking cancelled. Refund amount: ${refund_amount:.2f}",
                    "booking_id": booking_id,
                    "status": new_status
                }
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Waitlist --------------------
@app.post("/waitlist", status_code=status.HTTP_201_CREATED)
def add_to_waitlist(
    payload: WaitlistRequest,
    role: UserRole = Depends(require_ops_or_super)
):
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # Check if seats are still available
                cur.execute(
                    """
                    SELECT (sc.total_allocated + COALESCE(sc.overbooking_buffer, f.overbooking_buffer, 0) - sc.booked_count) AS available
                    FROM flight_seat_classes sc
                    JOIN flights f ON f.id = sc.flight_id
                    WHERE sc.flight_id = %s AND sc.class_name = %s
                    """,
                    (payload.flight_id, payload.seat_class.value)
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, "Flight or seat class not found")
                if row["available"] > 0:
                    raise HTTPException(400, "Seats available; use hold endpoint instead")

                # Insert into waitlist
                cur.execute(
                    """
                    INSERT INTO waitlist (flight_id, seat_class, passenger_name, email, loyalty_tier, fare_type, requested_at, status)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), 'Pending')
                    RETURNING id
                    """,
                    (payload.flight_id, payload.seat_class.value, payload.passenger_name,
                     payload.email, payload.loyalty_tier or 0, payload.fare_type.value)
                )
                waitlist_id = cur.fetchone()["id"]

                # Determine position in line (higher loyalty tier first, then older requested_at)
                cur.execute(
                    """
                    SELECT COUNT(*) as pos
                    FROM waitlist
                    WHERE flight_id = %s AND seat_class = %s AND status = 'Pending'
                      AND (loyalty_tier > %s OR (loyalty_tier = %s AND id <= %s))
                    """,
                    (payload.flight_id, payload.seat_class.value, payload.loyalty_tier or 0, payload.loyalty_tier or 0, waitlist_id)
                )
                pos_row = cur.fetchone()
                position = pos_row["pos"] if pos_row else 1

                audit_log(conn, "waitlist", waitlist_id, "ADD", changed_by=get_audit_user(role),
                          new_data=payload.model_dump(mode="json"))
                conn.commit()
                return {"waitlist_id": waitlist_id, "position": position}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Promote from Waitlist --------------------
@app.post("/waitlist/promote", status_code=status.HTTP_200_OK)
def promote_waitlist(
    payload: WaitlistPromoteRequest,
    role: UserRole = Depends(require_super_admin)
):
    with get_db_connection() as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                # Lock seat class row
                cur.execute(
                    """
                    SELECT sc.id, sc.total_allocated, COALESCE(sc.overbooking_buffer, f.overbooking_buffer, 0) as overbooking_buffer,
                           sc.booked_count, f.fare_type as default_fare_type
                    FROM flight_seat_classes sc
                    JOIN flights f ON f.id = sc.flight_id
                    WHERE sc.flight_id = %s AND sc.class_name = %s
                    FOR UPDATE OF sc
                    """,
                    (payload.flight_id, payload.seat_class.value)
                )
                seat_class = cur.fetchone()
                if not seat_class:
                    raise HTTPException(404, "Seat class unavailable")

                available = seat_class["total_allocated"] + seat_class["overbooking_buffer"] - seat_class["booked_count"]
                if available <= 0:
                    return {"message": "No seats available to promote", "promoted": []}

                num_to_promote = min(available, payload.max_promotions)

                # Fetch top eligible passengers
                cur.execute(
                    """
                    SELECT id, passenger_name, email, fare_type
                    FROM waitlist
                    WHERE flight_id = %s AND seat_class = %s AND status = 'Pending'
                    ORDER BY loyalty_tier DESC, requested_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (payload.flight_id, payload.seat_class.value, num_to_promote)
                )
                candidates = cur.fetchall()
                if not candidates:
                    return {"message": "No waitlisted passengers eligible for promotion", "promoted": []}

                promoted = []
                for cand in candidates:
                    cur.execute(
                        "UPDATE flight_seat_classes SET booked_count = booked_count + 1 WHERE id = %s",
                        (seat_class["id"],)
                    )
                    hold_expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
                    cand_fare = cand.get("fare_type") or seat_class.get("default_fare_type") or "BasicEconomy"
                    cur.execute(
                        """
                        INSERT INTO bookings (flight_id, seat_class, passenger_name, email, status, hold_expires_at, fare_type)
                        VALUES (%s, %s, %s, %s, 'Held', %s, %s)
                        RETURNING id
                        """,
                        (payload.flight_id, payload.seat_class.value, cand["passenger_name"],
                         cand["email"], hold_expiry, cand_fare)
                    )
                    booking_id = cur.fetchone()["id"]
                    cur.execute("UPDATE waitlist SET status = 'Promoted' WHERE id = %s", (cand["id"],))

                    promoted.append({
                        "waitlist_id": cand["id"],
                        "booking_id": booking_id,
                        "passenger_name": cand["passenger_name"],
                        "email": cand["email"]
                    })

                    if cand.get("email"):
                        send_email(
                            cand["email"],
                            f"Seat confirmed from waitlist for flight {payload.flight_id}",
                            f"Your temporary booking #{booking_id} has been created. Please confirm within 15 minutes."
                        )

                audit_log(conn, "waitlist", None, "PROMOTE", changed_by=get_audit_user(role),
                          new_data={"promoted": promoted})
                conn.commit()
                return {"message": f"Successfully promoted {len(promoted)} passenger(s)", "promoted": promoted}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Database error: {str(e)}")

# -------------------- Audit Logs Endpoint --------------------
@app.get("/audit", response_model=List[dict])
def get_audit_logs(
    table_name: Optional[str] = None,
    record_id: Optional[int] = None,
    limit: int = 100,
    role: UserRole = Depends(require_super_admin)
):
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            query = "SELECT id, table_name, record_id, action, changed_by, old_data, new_data, changed_at FROM audit_log"
            params = []
            conditions = []
            if table_name:
                conditions.append("table_name = %s")
                params.append(table_name)
            if record_id is not None:
                conditions.append("record_id = %s")
                params.append(record_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY changed_at DESC LIMIT %s"
            params.append(limit)
            cur.execute(query, params)
            rows = cur.fetchall()
            for r in rows:
                if r.get("changed_at"):
                    r["changed_at"] = r["changed_at"].isoformat()
            return rows

# ------------------------------------------------------------------
# Run: uvicorn main:app --reload
# ------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
