# Hugging Face Space (Docker SDK): the demo server and the committed page, no models. Every model call goes to the
# hosted vllm-sr API: add VLLM_SR_API_KEY as a Space secret (never as a variable or in this file). TRUST_PROXY=1 makes
# the per-visitor limits use the client address the Space proxy forwards.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 PORT=7860 PLAY_ALL=0 LANDING_URL= TRUST_PROXY=1 \
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
