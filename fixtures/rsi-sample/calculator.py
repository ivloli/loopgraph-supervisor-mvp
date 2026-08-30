def average(values: list[float]) -> float:
    """Return the arithmetic mean of a list of numbers.

    An empty sequence returns 0.0; passing None raises TypeError.
    """
    if values is None:
        raise TypeError("average() expects a list of numbers, got None")
    size = len(values)
    if size == 0:
        return 0.0
    return sum(values) / size
