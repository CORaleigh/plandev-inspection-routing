if __package__:
    from .inspection_routing.cli import estimate_main
else:
    from inspection_routing.cli import estimate_main


if __name__ == "__main__":
    estimate_main()
