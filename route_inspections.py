if __package__:
    from .inspection_routing.cli import main
else:
    from inspection_routing.cli import main


if __name__ == "__main__":
    main()
