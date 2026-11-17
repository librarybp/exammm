# Exam Seat Plan System — Barpeta Polytechnic

Flask + SQLite. No build step. Public seat search at `/find`, admin at `/login`.

**Default login:** `admin` / `admin@123` (or the `ADMIN_PASSWORD` set on first run). Change it in **Settings**.

## Features
- **Rooms:** room no, building, floor, rows × columns, seat capacity, accessible flag, active/inactive
- **Students:** add by roll-number range (prefix + start–end + zero-pad), CSV import (Assamese names OK), single add/edit, PwD flag
- **Exams:** date, session, time, multiple branch/semester/paper groups, room selection with fill order and invigilators
- **Arrangements:** column-wise alternate branch, checkerboard, sequential; optional gap seating (alternate columns empty)
- **Rules:** exclude detained roll numbers; PwD candidates seated first in front rows of accessible rooms; room clash warning for same date/session; capacity shortfall warning
- **Printouts (A4):** colour seat chart per room, branch-wise notice board, room door list, attendance sheet with booklet no. and signature, desk stickers
- **Export:** CSV for Excel. **Duplicate** an exam for the next paper/day.

## Deploy on Lightsail (Ubuntu)
```bash
sudo mkdir -p /var/www/seatplan && sudo chown ubuntu:www-data /var/www/seatplan
git clone https://github.com/<your-username>/exam-seatplan.git /var/www/seatplan
cd /var/www/seatplan
python3 -m venv venv && venv/bin/pip install -r requirements.txt
sudo cp deploy/seatplan.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now seatplan
sudo cp deploy/seatplan.nginx /etc/nginx/sites-available/seatplan
sudo ln -s /etc/nginx/sites-available/seatplan /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d seatplan.bpoly.ac.in
```
Edit `server_name`, port `5012`, and `ADMIN_PASSWORD` as needed. Database: `seatplan.db` (back up this file). Local test: `python3 app.py` → http://localhost:5000

## Update the server from GitHub
```bash
cd /var/www/seatplan && git pull && sudo systemctl restart seatplan
```
