if __package__:
    from .inspection_routing.publishing import main
else:
    from inspection_routing.publishing import main


if __name__ == "__main__":
    main()
