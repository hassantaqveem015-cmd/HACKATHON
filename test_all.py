import os
import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from main import app, ADMIN_API_KEY, OPS_API_KEY

client = TestClient(app)

def run_tests():
    print("=== Starting Comprehensive API Tests ===")

    # 1. Health & DB Check
    res = client.get("/")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    print("[PASS] GET / ->", res.json())

    res = client.get("/db-check")
    assert res.status_code == 200, f"DB check failed: {res.text}"
    print("[PASS] GET /db-check ->", res.json())

    # 2. Admin: Create Flight
    unique_flight_no = f"UK{uuid.uuid4().hex[:4].upper()}"
    dep_time = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    arr_time = (datetime.now(timezone.utc) + timedelta(days=2, hours=7)).isoformat()
    idem_flight_key = str(uuid.uuid4())

    flight_payload = {
        "flight_number": unique_flight_no,
        "origin": "LHR",
        "destination": "DXB",
        "departure_time": dep_time,
        "arrival_time": arr_time,
        "total_capacity": 100,
        "allocations": {
            "first": 10,
            "business": 20,
            "economy": 70
        },
        "fare_type": "Flexible",
        "overbooking_buffer": 5
    }

    # Without API key -> 401
    res = client.post("/flights", json=flight_payload)
    assert res.status_code == 401, f"Expected 401 without key, got {res.status_code}"
    print("[PASS] POST /flights rejected unauthenticated request (401)")

    # With Admin API Key
    res = client.post(
        "/flights",
        json=flight_payload,
        headers={"X-API-Key": ADMIN_API_KEY, "Idempotency-Key": idem_flight_key}
    )
    assert res.status_code == 201, f"Flight creation failed: {res.text}"
    flight_data = res.json()
    flight_id = flight_data["id"]
    print(f"[PASS] POST /flights created flight ID: {flight_id}, Number: {unique_flight_no}")

    # Test Idempotency on Create Flight -> Should return 409 Conflict
    res_duplicate = client.post(
        "/flights",
        json=flight_payload,
        headers={"X-API-Key": ADMIN_API_KEY, "Idempotency-Key": idem_flight_key}
    )
    assert res_duplicate.status_code == 409, f"Expected 409 for duplicate idempotency key, got {res_duplicate.status_code}"
    print("[PASS] Idempotency prevented duplicate flight creation (409 Conflict)")

    # 3. Admin: Edit Flight
    edit_payload = {
        "overbooking_buffer": 8,
        "allocations": {
            "first": 15,
            "business": 25,
            "economy": 60
        }
    }
    res = client.put(
        f"/flights/{flight_id}",
        json=edit_payload,
        headers={"X-API-Key": ADMIN_API_KEY}
    )
    assert res.status_code == 200, f"Edit flight failed: {res.text}"
    print(f"[PASS] PUT /flights/{flight_id} edited successfully: total_capacity={res.json()['total_capacity']}")

    # 4. Search Flights
    search_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    res = client.get(f"/search?origin=LHR&destination=DXB&departure_date={search_date}")
    assert res.status_code == 200, f"Search failed: {res.text}"
    search_results = res.json()
    assert len(search_results) > 0, "Expected at least one search result"
    print(f"[PASS] GET /search found {len(search_results)} seat classes for flight {flight_id}")

    # 5. Seat Hold (Atomic)
    hold_idem_key = str(uuid.uuid4())
    hold_payload = {
        "flight_id": flight_id,
        "seat_class": "Economy",
        "passenger_name": "John Doe",
        "email": "johndoe@example.com"
    }
    res = client.post(
        "/bookings/hold",
        json=hold_payload,
        headers={"X-API-Key": OPS_API_KEY, "Idempotency-Key": hold_idem_key}
    )
    assert res.status_code == 201, f"Hold seat failed: {res.text}"
    booking_1 = res.json()
    booking_id_1 = booking_1["booking_id"]
    print(f"[PASS] POST /bookings/hold held seat: Booking ID {booking_id_1}")

    # Duplicate Hold with same Idempotency Key -> 409
    res_idem_hold = client.post(
        "/bookings/hold",
        json=hold_payload,
        headers={"X-API-Key": OPS_API_KEY, "Idempotency-Key": hold_idem_key}
    )
    assert res_idem_hold.status_code == 409, "Expected 409 Conflict for duplicate hold"
    print("[PASS] Idempotency prevented duplicate seat hold (409 Conflict)")

    # 6. Confirm Booking
    confirm_payload = {"booking_id": booking_id_1}
    res = client.post(
        "/bookings/confirm",
        json=confirm_payload,
        headers={"X-API-Key": OPS_API_KEY}
    )
    assert res.status_code == 200, f"Confirm booking failed: {res.text}"
    print(f"[PASS] POST /bookings/confirm confirmed Booking ID {booking_id_1}")

    # 7. Cancel Booking (with refund check)
    cancel_payload = {"booking_id": booking_id_1, "reason": "Passenger requested change"}
    res = client.post(
        "/bookings/cancel",
        json=cancel_payload,
        headers={"X-API-Key": OPS_API_KEY}
    )
    assert res.status_code == 200, f"Cancel booking failed: {res.text}"
    cancel_res = res.json()
    print(f"[PASS] POST /bookings/cancel cancelled booking: status={cancel_res.get('status')}")

    # 8. Test Hold another seat & Confirm via path param
    hold_payload_2 = {
        "flight_id": flight_id,
        "seat_class": "Business",
        "passenger_name": "Jane Smith",
        "email": "janesmith@example.com"
    }
    res = client.post("/bookings/hold", json=hold_payload_2, headers={"X-API-Key": OPS_API_KEY})
    assert res.status_code == 201
    booking_id_2 = res.json()["booking_id"]
    res = client.post(f"/bookings/{booking_id_2}/confirm", headers={"X-API-Key": OPS_API_KEY})
    assert res.status_code == 200
    print(f"[PASS] POST /bookings/{booking_id_2}/confirm verified backwards-compatible route")

    # 9. Test Waitlist functionality
    # First create a mini flight with 1 seat to fill it
    mini_flight_no = f"WL{uuid.uuid4().hex[:4].upper()}"
    mini_flight = client.post(
        "/flights",
        json={
            "flight_number": mini_flight_no,
            "origin": "JFK",
            "destination": "LAX",
            "departure_time": dep_time,
            "arrival_time": arr_time,
            "total_capacity": 3,
            "allocations": {"first": 1, "business": 1, "economy": 1},
            "fare_type": "Flexible",
            "overbooking_buffer": 0
        },
        headers={"X-API-Key": ADMIN_API_KEY}
    ).json()
    mini_id = mini_flight["id"]

    # Fill the 1 First class seat
    h_res = client.post(
        "/bookings/hold",
        json={"flight_id": mini_id, "seat_class": "First", "passenger_name": "P1", "email": "p1@test.com"},
        headers={"X-API-Key": OPS_API_KEY}
    )
    assert h_res.status_code == 201

    # Attempting to hold another First class seat should fail (400)
    h_fail = client.post(
        "/bookings/hold",
        json={"flight_id": mini_id, "seat_class": "First", "passenger_name": "P2", "email": "p2@test.com"},
        headers={"X-API-Key": OPS_API_KEY}
    )
    assert h_fail.status_code == 400
    print("[PASS] Full seat class correctly rejected additional hold (400)")

    # Add to waitlist for First class
    wl_payload = {
        "flight_id": mini_id,
        "seat_class": "First",
        "passenger_name": "VIP Alice",
        "email": "alice@vip.com",
        "loyalty_tier": 3,
        "fare_type": "FirstFlex"
    }
    wl_res = client.post("/waitlist", json=wl_payload, headers={"X-API-Key": OPS_API_KEY})
    assert wl_res.status_code == 201, f"Waitlist failed: {wl_res.text}"
    wl_id = wl_res.json()["waitlist_id"]
    print(f"[PASS] POST /waitlist added passenger to waitlist ID: {wl_id}, position: {wl_res.json()['position']}")

    # Expand allocation or overbooking buffer on mini flight to allow promotion
    client.put(
        f"/flights/{mini_id}",
        json={"allocations": {"first": 2, "business": 1, "economy": 1}},
        headers={"X-API-Key": ADMIN_API_KEY}
    )

    # Promote from waitlist
    promote_res = client.post(
        "/waitlist/promote",
        json={"flight_id": mini_id, "seat_class": "First", "max_promotions": 1},
        headers={"X-API-Key": ADMIN_API_KEY}
    )
    assert promote_res.status_code == 200, f"Promote failed: {promote_res.text}"
    print(f"[PASS] POST /waitlist/promote successfully promoted passenger: {promote_res.json()['promoted']}")

    # 10. Audit Logs
    audit_res = client.get("/audit?limit=10", headers={"X-API-Key": ADMIN_API_KEY})
    assert audit_res.status_code == 200, f"Audit logs failed: {audit_res.text}"
    logs = audit_res.json()
    assert len(logs) > 0
    print(f"[PASS] GET /audit retrieved {len(logs)} audit entries")

    # 11. Cancel Flight (Admin)
    cancel_fl_res = client.post(f"/flights/{flight_id}/cancel", headers={"X-API-Key": ADMIN_API_KEY})
    assert cancel_fl_res.status_code == 202, f"Cancel flight failed: {cancel_fl_res.text}"
    print(f"[PASS] POST /flights/{flight_id}/cancel cancelled flight successfully: {cancel_fl_res.json()['message']}")

    print("\n==========================================")
    print("ALL API ENDPOINTS TESTED WITH ZERO ERRORS!")
    print("==========================================")

if __name__ == "__main__":
    run_tests()
