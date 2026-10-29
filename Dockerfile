FROM python:3.12-slim
WORKDIR /app
# glibc по умолчанию заводит malloc-арену на каждый поток (до 8×ядер): после
# параллельного разбора страниц RSS не возвращается. Две арены — стандартная
# настройка для Python-воркеров с потоками.
ENV MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072 \
    PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "app.main"]
