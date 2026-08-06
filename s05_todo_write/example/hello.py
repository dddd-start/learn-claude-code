"""A simple greeting module."""


def greet(name: str) -> None:
    """Print a greeting message for the given name.

    Args:
        name: The name of the person to greet.
    """
    message = "Hello, " + name
    print(message)


if __name__ == "__main__":
    greet("Claude")
