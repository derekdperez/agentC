"""Simple mathematical utility functions."""

from __future__ import annotations


def add(a: int | float, b: int | float) -> int | float:
    """Return the sum of *a* and *b*."""
    return a + b


def factorial(n: int) -> int:
    """Return the factorial of *n*."""
    if n < 0:
        raise ValueError("factorial not defined for negative integers")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


def square(n: int | float) -> int | float:
    """Return the square of *n*."""
    return n * n


def absolute(n: int | float) -> int | float:
    """Return the absolute value of *n*."""
    return -n if n < 0 else n


def max_of_two(a: int | float, b: int | float) -> int | float:
    """Return the maximum of *a* and *b*."""
    return a if a >= b else b