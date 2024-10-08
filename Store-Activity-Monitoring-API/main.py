from fastapi import FastAPI, Depends, HTTPException, status
from database import engine, get_db
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timedelta, timezone
from fastapi.responses import FileResponse
from pathlib import Path
import schemas
import models
import pytz
import csv

app = FastAPI()

models.Base.metadata.create_all(engine)

# Valid Timezone


def is_valid_timezone(timezone):
    try:
        pytz.timezone(timezone)
        return True
    except pytz.exceptions.UnknownTimeZoneError:
        return False


@app.put("/register_store")
def register_store(request: schemas.store,  db: Session = Depends(get_db)):
    store = models.Store(id=request.id)
    if is_valid_timezone(request.local_timezone):
        store.local_timezone = request.local_timezone

    # Schedule Iterator
    for weekday in request.schedule:
        if 0 <= weekday.day <= 6:
            week_day = models.BusinessHours(start_time_local=weekday.start_time_local,
                                            end_time_local=weekday.end_time_local, day=weekday.day)
            store.schedule.append(week_day)
            db.add(week_day)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Week Day")

    # Commit Model
    try:
        db.add(store)
        db.commit()
    except SQLAlchemyError as msg:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(msg.__dict__['orig']))
    return {"Message": "Store Added"}


def get_bussineess(db, store, day):
    try:
        business_hours_record = db.query(models.BusinessHours).join(models.Store).filter(
            models.BusinessHours.store == store, models.BusinessHours.day == day).one()
    except:
        business_hours_record = models.BusinessHours(day=day)
        store.schedule.append(business_hours_record)
        db.add(business_hours_record)
        db.commit()
        print("created -- weekday")

    return business_hours_record


def create_default(db, store, local_timestamp):
    # Create Week if Not Found
    if not store.week_info:
        week = models.Week(current_week=local_timestamp.date())
        db.add(week)
        store.week_info = week
        db.commit()
    # Create Day if Not Found
    if not store.day_info:
        day = models.Day(current_date=local_timestamp.date())
        db.add(day)
        store.day_info = day
        db.commit()


@ app.post("/poll/")
def poll(request: schemas.poll, db: Session = Depends(get_db)):
    store = db.query(models.Store).get(request.id)
    if not store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invalid Store Id")
    # Convert UTC to local time zone
    if request.utc_timestamp > datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Timestamp must be less than {datetime.now(timezone.utc)} UTC")
    local_timestamp = store.convert_to_local(request.utc_timestamp)

    create_default(db, store, local_timestamp)

    store_previous_local_poll = store.previous_poll
    local_timestamp = local_timestamp.replace(tzinfo=None)

    if store_previous_local_poll >= local_timestamp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Timestamp must be greater than {store.previous_poll}")

    # Save TimeStamp
    try:
        hour = models.Hour(timestamp_local=local_timestamp,
                           status=request.status)
        db.add(hour)
        store.hour_info.append(hour)
        db.commit()
    except:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Timestamp already found")

    # Adjust prev and current value
    # Remove unecessary hours
    delete_unecessary(db, store, local_timestamp)
    store.week_info.adjust_week(local_timestamp)
    store.day_info.adjust_day(local_timestamp)
    db.commit()
    # Calculate
    # Get business object
    business_hours_record = get_bussineess(
        db, store, day=local_timestamp.weekday())

    if request.status == '1' and business_hours_record.check_time_in_busi(local_timestamp):
        difference_days = (local_timestamp.date(
        ) - store_previous_local_poll.date()).days
        # Check Difference between previous and current
        # # previous poll not on same day subtract from starting time
        print(f'{local_timestamp} ---- {difference_days}')

        if difference_days == 0:
            uptime_duration = (local_timestamp - store_previous_local_poll)
        else:
            tmp_start_time = local_timestamp
            start_time_local = datetime.combine(
                tmp_start_time, business_hours_record.start_time_local)

            uptime_duration = local_timestamp - start_time_local
        print(uptime_duration)
        store.day_info.current_day_uptime += uptime_duration
        store.week_info.current_week_uptime += uptime_duration

    # Update previous poll
    store.previous_poll = local_timestamp
    db.commit()
    # 2023-01-22 12:09:39.388884
    raise HTTPException(status_code=status.HTTP_202_ACCEPTED,
                        detail="Poll Sucessfull")


def calculate_last_hour(db, store, local_timestamp):
    start_of_last_hour = (local_timestamp - timedelta(hours=1)
                          ).replace(minute=0, second=0, microsecond=0).replace(tzinfo=None)
    # end of last hour is the start of the current hour
    end_of_last_hour = local_timestamp.replace(
        minute=0, second=0, microsecond=0).replace(tzinfo=None)
    print(local_timestamp)

    results = db.query(models.Hour).join(models.Store).filter(
        models.Hour.store == store,
        models.Hour.timestamp_local > start_of_last_hour
    ).order_by(models.Hour.timestamp_local).all()
    uptime_duration = timedelta(0)
    print(results)

    business_hours_record = get_bussineess(
        db, store, day=local_timestamp.weekday())

    if results == []:
        print(store)
        return uptime_duration

    previous_time = start_of_last_hour

    if business_hours_record.start_time_local > start_of_last_hour.time():
        previous_time = datetime.combine(
            local_timestamp, business_hours_record.start_time_local)
    print(local_timestamp)
    print(start_of_last_hour)
    print(end_of_last_hour)

    for result in results:
        if result.timestamp_local > end_of_last_hour:
            # Handle condition for 24/7
            if result.status == 1 and (end_of_last_hour.date() - result.timestamp_local.date()).days == 0:
                uptime_duration += end_of_last_hour - previous_time
                break
        elif result.status == 1:
            uptime_duration += result.timestamp_local - previous_time

        previous_time = result.timestamp_local

    print(store.id, uptime_duration)
    return uptime_duration


def get_week_time(db, store):
    duration = timedelta(0)
    for day in range(7):
        duration += get_bussineess(db, store, day).total_time()
    return duration


def timestamp_to_str(delta) -> str:
    return "{0} days, {1}:{2:02d}:{3:02d}".format(delta.days, delta.seconds // 3600, (delta.seconds // 60) % 60, delta.seconds % 60)


def delete_unecessary(db, store, local_timestamp):
    start_of_last_hour = (local_timestamp - timedelta(hours=1)
                          ).replace(minute=0, second=0, microsecond=0).replace(tzinfo=None)
    db.query(models.Hour).filter(models.Hour.store == store,
                                 models.Hour.timestamp_local < start_of_last_hour).delete()
    db.commit()


# For testing with given time
# # Change .get to .put
# # add request: schemas.give_stmp
@ app.get("/trigger_report")
def trigger_report(db: Session = Depends(get_db)):
    stores = db.query(models.Store).all()
    utc_time = datetime.now(timezone.utc)
    report_id = f"{utc_time.strftime('%Y-%m-%d_%H-%M-%S')}"

    with open(f"Data/{report_id}.csv", "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        # Header
        writer.writerow(["Store ID", "Uptime Last Hour", "Uptime Last Day", "Uptime Last Week",
                        "Downtime Last Hour", "Downtime Last Day", "Downtime Last Week"])
        for store in stores:
            local_timestamp = store.convert_to_local(
                utc_time)

            # if request.utc_timestamp:
            #     local_timestamp = store.convert_to_local(request.utc_timestamp)

            create_default(db, store, local_timestamp)
            # Upadate Values
            store.week_info.adjust_week(local_timestamp)
            store.day_info.adjust_day(local_timestamp)

            delete_unecessary(db, store, local_timestamp)
            uptime_last_hour = calculate_last_hour(db, store, local_timestamp)
            uptime_last_day = store.day_info.previous_day_uptime
            uptime_last_week = store.week_info.previous_week_uptime

            downtime_last_hour = timestamp_to_str(
                timedelta(seconds=3600) - uptime_last_hour)
            downtime_last_day = timestamp_to_str(get_bussineess(
                db, store, local_timestamp.weekday()-1).total_time() - uptime_last_day)
            downtime_last_week = timestamp_to_str(get_week_time(
                db, store) - uptime_last_week)

            writer.writerow([store.id, timestamp_to_str(uptime_last_hour), timestamp_to_str(
                uptime_last_day), timestamp_to_str(uptime_last_week), downtime_last_hour, downtime_last_day, downtime_last_week])

    raise HTTPException(status_code=status.HTTP_201_CREATED,
                        detail={"report_id": report_id})


@app.get('/get_report/{report_id}')
def get_report(report_id: str):
    # specify the path to your CSV file
    file_path = f"Data/{report_id}.csv"
    # check if the file exists
    if not Path(file_path).is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Running")

    # create a response object with the CSV file
    response = FileResponse(file_path, media_type="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=data.csv"
    response.headers["Detail"] = "Completed"

    return response

# Assumptions
# start_time -------  14:00 xxxxxxx endtime

# last_hour mean prevoius one hour example --------- 7:00 - 8:00 etc for 8:13
# last_day mean previous day from ------------- 23rd start_time to end_time for 24th day
# last_week mean previous week from ----------- last Monday to Sunday
# Time only related to current date in case of 24/7
