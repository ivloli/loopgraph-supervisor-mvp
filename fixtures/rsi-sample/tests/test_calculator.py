from calculator import average


def test_average_values():
    assert average([2, 4, 6]) == 4


def test_average_empty_values_returns_zero():
    assert average([]) == 0
