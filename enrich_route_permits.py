if __package__:
    from .inspection_routing.permit_enrichment import main
else:
    from inspection_routing.permit_enrichment import main


if __name__ == "__main__":
    main()
