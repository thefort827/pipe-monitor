FROM python:3.12

WORKDIR /app

# Install Chinese fonts for matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

# Clear matplotlib font cache
RUN python -c "import matplotlib; import shutil; shutil.rmtree(matplotlib.get_cachedir(), ignore_errors=True)"

# Use Chinese pip mirror
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    flask openpyxl pyproj pandas scipy numpy requests \
    APScheduler==3.10.4 openai matplotlib pycryptodome

COPY . .

EXPOSE 5000

CMD ["python", "-u", "app.py"]
