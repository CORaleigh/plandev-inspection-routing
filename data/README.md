# Reference data

`holidays.csv` is generated from EnerGov's `HOLIDAY` table by running:

```
python poc\export_holidays.py
```

The generated CSV contains only `HolidayDate` and `Name`, is intentionally not
ignored, and should be committed so scheduled runs can determine business days
without connecting to the reporting database.
