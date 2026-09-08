FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt
COPY turnout/ turnout/
EXPOSE 8100
CMD ["uvicorn", "turnout.main:app", "--host", "0.0.0.0", "--port", "8100"]
