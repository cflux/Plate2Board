from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def example_plate_svg() -> str:
    return (FIXTURES / "example_plate.svg").read_text()


@pytest.fixture
def complex_example_svg() -> str:
    return (FIXTURES / "complex_example.svg").read_text()
