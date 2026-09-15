FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# The timetable database lives on a volume so a container restart does not mean
# re-downloading 300 MB of GTFS.
VOLUME ["/app/data"]
EXPOSE 8022

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "-b", "0.0.0.0:8022", "--timeout", "120", "app.api:app"]
