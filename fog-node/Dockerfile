FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scoring.py fog_node.py aws_publisher.py ./

CMD ["python", "fog_node.py"]
