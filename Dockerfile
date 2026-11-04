# Hugging Face Space (Docker SDK): the demo server and the committed page, no models. The model APIs are reached over
# HTTP: set ROUTER_URL and ROUTER_ADMIN_URL as Space variables (see deploy/space/README.md).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 PORT=7860 PLAY_ALL=0 LANDING_URL= \
    MALLOC_ARENA_MAX=2

# Spaces run the container as uid 1000
RUN useradd -m -u 1000 user
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user app/ app/
COPY --chown=user data/ data/
COPY --chown=user web/ web/

USER user
EXPOSE 7860
CMD ["python", "app/server.py"]
