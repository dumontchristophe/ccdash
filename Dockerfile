FROM python:3.12-alpine

COPY ccdash/ /app/ccdash/
WORKDIR /app

EXPOSE 4318
ENTRYPOINT ["python3","-m","ccdash","--host","0.0.0.0","--db","/data/ccdash.db"]
