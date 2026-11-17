from datetime import datetime, timedelta

def get_business_dates() -> dict:
    """
    Calculates today's date, the previous work day, and the next work day.
    Automatically accounts for weekend skips (Saturday and Sunday).

    Returns:
        dict: Dates formatted as 'YYYY-MM-DD' for 'today', 'prev_work_day', and 'next_work_day'.
    """
    today = datetime.now().date()

    # Calculate previous business day (skip weekend)
    if today.weekday() == 0:    # Monday -> Friday
        prev_work_day = today - timedelta(days=3)
    elif today.weekday() == 6:  # Sunday -> Friday
        prev_work_day = today - timedelta(days=2)
    else:
        prev_work_day = today - timedelta(days=1)

    # Calculate next business day (skip weekend)
    if today.weekday() == 4:    # Friday -> Monday
        next_work_day = today + timedelta(days=3)
    elif today.weekday() == 5:  # Saturday -> Monday
        next_work_day = today + timedelta(days=2)
    else:
        next_work_day = today + timedelta(days=1)

    return {
        "today": today.strftime("%Y-%m-%d"),
        "prev_work_day": prev_work_day.strftime("%Y-%m-%d"),
        "next_work_day": next_work_day.strftime("%Y-%m-%d")
    }