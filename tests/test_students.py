"""Student endpoints."""

from uuid import uuid4

from httpx import AsyncClient

from app.models.student import Student


async def test_create_student(client: AsyncClient) -> None:
    response = await client.post(
        "/students",
        json={"display_name": "Rifat", "education_level": "Class 8"},
    )
    assert response.status_code == 201

    body = response.json()
    assert body["display_name"] == "Rifat"
    assert body["education_level"] == "Class 8"
    assert body["id"]


async def test_education_level_accepts_any_school_system(client: AsyncClient) -> None:
    """No fixed grade list — a university student must have a valid answer."""
    for level in ("Class 8", "Grade 7", "HSC 1st year", "University, 2nd year", "AP"):
        response = await client.post(
            "/students", json={"display_name": "Test", "education_level": level}
        )
        assert response.status_code == 201, f"{level} should be accepted"
        assert response.json()["education_level"] == level


async def test_education_level_is_required(client: AsyncClient) -> None:
    """Every tutor prompt reads it, so it cannot be missing."""
    response = await client.post("/students", json={"display_name": "Rifat"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_blank_fields_are_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/students", json={"display_name": "  ", "education_level": "Class 8"}
    )
    assert response.status_code == 422


async def test_me_echoes_the_header_student(
    client: AsyncClient, student: Student, auth: dict[str, str]
) -> None:
    response = await client.get("/students/me", headers=auth)
    assert response.status_code == 200
    assert response.json()["id"] == str(student.id)


async def test_me_rejects_an_unknown_student(client: AsyncClient) -> None:
    response = await client.get("/students/me", headers={"X-Student-Id": str(uuid4())})
    assert response.status_code == 404


async def test_me_rejects_a_malformed_student_id(client: AsyncClient) -> None:
    response = await client.get("/students/me", headers={"X-Student-Id": "not-a-uuid"})
    assert response.status_code == 422
