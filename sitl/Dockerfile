# The ground side of the SITL runs: a MAVLink client that loads a parameter
# file, flies the mission and logs telemetry, plus what analyze.py needs to
# draw the result. PX4 itself runs in a separate container (see run.sh).
FROM python:3.12-slim
RUN pip install --no-cache-dir pymavlink==2.4.43 matplotlib==3.9.2 pillow==11.0.0
