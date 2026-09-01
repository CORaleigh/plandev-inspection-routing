if __package__:
    from .inspection_routing.cli import holiday_export_main
else:
    from inspection_routing.cli import holiday_export_main


if __name__ == "__main__":
    holiday_export_main()
