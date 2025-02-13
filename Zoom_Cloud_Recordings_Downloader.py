#!/usr/bin/env python3

# Program Name: zoom-recording-downloader.py
# Description:  Zoom Recording Downloader is a cross-platform Python script
#               that uses Zoom's API (v2) to download and organize all
#               cloud recordings from a Zoom account onto local storage.
#               This Python script uses the OAuth method of accessing the Zoom API
# Created:      2023-11-14
# Author:       Alejandro Morales Hellin
# Website:      https://github.com/Vizardx/Zoom_Cloud_Recordings_Downloader
# Forked from:  https://github.com/ricardorodrigues-ca/zoom-recording-downloader
                
# system libraries
import base64
import datetime
import json
import os
import re as regex
import signal
import sys as system
import time


# installed libraries
import dateutil.parser as parser
import pathvalidate as path_validate
import requests
import tqdm as progress_bar
import pandas as pd
from retrying import retry
# import gc     -Use if needed

CONF_PATH = "Zoom_Cloud_Recordings_Downloader.conf"
with open(CONF_PATH, encoding="utf-8-sig") as json_file:
    CONF = json.loads(json_file.read())

ACCOUNT_ID = CONF["OAuth"]["account_id"]
CLIENT_ID = CONF["OAuth"]["client_id"]
CLIENT_SECRET = CONF["OAuth"]["client_secret"]

APP_VERSION = "4.0 (OAuth)"

API_ENDPOINT_USER_LIST = "https://api.zoom.us/v2/users"

# Set these variables to the earliest recording date you wish to download
RECORDING_START_YEAR = 2020
RECORDING_START_MONTH = 1
RECORDING_START_DAY = 1
RECORDING_END_DATE = datetime.date.today() #or datetime.date()
DOWNLOAD_DIRECTORY = 'Downloads'
COMPLETED_MEETING_IDS_LOG = 'completed-downloads.log'
COMPLETED_MEETING_IDS = set()
global start_time
start_time = time.time()


class Color:
    PURPLE = "\033[95m"
    CYAN = "\033[96m"
    DARK_CYAN = "\033[36m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    WHITE = "\033[97m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"

@retry(retry_on_exception=retry_if_connection_error, wait_exponential_multiplier=1000, wait_exponential_max=480000)
def load_access_token():
    """ OAuth function, thanks to https://github.com/freelimiter
    """
    url = f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={ACCOUNT_ID}"

    client_cred = f"{CLIENT_ID}:{CLIENT_SECRET}"
    client_cred_base64_string = base64.b64encode(client_cred.encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Basic {client_cred_base64_string}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    response = json.loads(requests.request("POST", url, headers=headers).text)

    global ACCESS_TOKEN
    global AUTHORIZATION_HEADER

    try:
        ACCESS_TOKEN = response["access_token"]
        AUTHORIZATION_HEADER = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

    except KeyError:
        print(f"{Color.RED}### The key 'access_token' wasn't found.{Color.END}")

@retry(retry_on_exception=retry_if_connection_error, wait_exponential_multiplier=1000, wait_exponential_max=480000)
def get_users():
    """ loop through pages and return all users
    """
    response = requests.get(url=API_ENDPOINT_USER_LIST, headers=AUTHORIZATION_HEADER)

    if not response.ok:
        print(response)
        print(
            f"{Color.RED}### Could not retrieve users. Please make sure that your access "
            f"token is still valid{Color.END}"
        )

        system.exit(1)

    page_data = response.json()
    total_pages = int(page_data["page_count"]) + 1

    all_users = []

    for page in range(1, total_pages):
        url = f"{API_ENDPOINT_USER_LIST}?page_number={str(page)}"
        user_data = requests.get(url=url, headers=AUTHORIZATION_HEADER).json()
        users = ([
            (
                user["email"],
                user["id"],
                user["first_name"],
                user["last_name"]
            )
            for user in user_data["users"]
        ])

        all_users.extend(users)
        page += 1

    return all_users


def format_filename(params):
    file_extension = params["file_extension"]
    recording = params["recording"]
    recording_id = params["recording_id"]
    recording_type = params["recording_type"]

    invalid_chars_pattern = r'[<>:"/\\|?*\x00-\x1F]'
    topic = regex.sub(invalid_chars_pattern, '', recording["topic"])
    rec_type = recording_type.replace("_", " ").title()
    meeting_time = parser.parse(recording["start_time"]).strftime("%Y.%m.%d - %I.%M %p UTC")

    return (
        f"{meeting_time} - {topic} - {rec_type} - {recording_id}.{file_extension.lower()}",
        f"{topic} - {meeting_time}"
    )


def get_downloads(recording):
    if not recording.get("recording_files"):
        raise Exception

    downloads = []
    for download in recording["recording_files"]:
        file_type = download["file_type"]
        file_extension = download["file_extension"]
        recording_id = download["id"]

        if file_type == "":
            recording_type = "incomplete"
        elif file_type != "TIMELINE":
            recording_type = download["recording_type"]
        else:
            recording_type = download["file_type"]

        # must append access token to download_url
        download_url = f"{download['download_url']}?access_token={ACCESS_TOKEN}"
        downloads.append((file_type, file_extension, download_url, recording_type, recording_id))

    return downloads


def get_recordings(email, page_size, rec_start_date, rec_end_date):
    return {
        "userId": email,
        "page_size": page_size,
        "from": rec_start_date,
        "to": rec_end_date
    }


def per_delta(start, end, delta):
    """ Generator used to create deltas for recording start and end dates
    """
    curr = start
    while curr < end:
        yield curr, min(curr + delta, end)
        curr += delta

@retry(retry_on_exception=retry_if_connection_error, wait_exponential_multiplier=1000, wait_exponential_max=480000)
def list_recordings(email):
    """ Start date now split into YEAR, MONTH, and DAY variables (Within 6 month range)
        then get recordings within that range
    """
    recordings = []

    for start, end in per_delta(
        datetime.date(RECORDING_START_YEAR, RECORDING_START_MONTH, RECORDING_START_DAY),
        RECORDING_END_DATE,
        datetime.timedelta(days=30)
    ):
        post_data = get_recordings(email, 300, start, end)
        response = requests.get(
            url=f"https://api.zoom.us/v2/users/{email}/recordings",
            headers=AUTHORIZATION_HEADER,
            params=post_data
        )
        recordings_data = response.json()
        recordings.extend(recordings_data["meetings"])

    return recordings

@retry(retry_on_exception=retry_if_connection_error, wait_exponential_multiplier=1000, wait_exponential_max=480000)
def download_recording(download_url, email, filename, folder_name):
    """
    Download a recording to the specified path.
    """
    # Check if the file is a .mp4 file
    if not filename.endswith('.mp4'):
        return False

    # Get the user name from the email
    user_name = email.split('@')[0]
    # Define the download directory for this user
    user_folder = f"{DOWNLOAD_DIRECTORY}/{user_name}"
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    dl_dir = os.sep.join([user_folder, folder_name])
    sanitized_download_dir = path_validate.sanitize_filepath(dl_dir)
    sanitized_filename = path_validate.sanitize_filename(filename)
    full_filename = os.sep.join([sanitized_download_dir, sanitized_filename])

    os.makedirs(sanitized_download_dir, exist_ok=True)

    response = requests.get(download_url, stream=True)

    # total size in bytes.
    total_size = int(response.headers.get("content-length", 0))
    block_size = 1 * 1024 * 1024

    # create TQDM progress bar
    prog_bar = progress_bar.tqdm(total=total_size, unit="iB", unit_scale=True)
    
    recording = {}  # Crear un nuevo diccionario para almacenar la información de la grabación
    recording['user_name'] = user_name  # Agregar el nombre de usuario al diccionario
    
    try:
        with open(full_filename, "wb") as fd:
            for chunk in response.iter_content(block_size):
                prog_bar.update(len(chunk))
                fd.write(chunk)  # write video chunk to disk
        prog_bar.close()

        global start_time
        if time.time() - start_time >= 55 * 60:
            # Han pasado 55 minutos desde el inicio del script
            #gc.collect()
            load_access_token()
            start_time = time.time()

        return recording  # Devolver el diccionario con la información de la grabación

    except Exception as e:
        print(
            f"{Color.RED}### The video recording with filename '{filename}' for user with email "
            f"'{email}' could not be downloaded because {Color.END}'{e}'"
        )

        return False



def load_completed_meeting_ids():
    try:
        with open(COMPLETED_MEETING_IDS_LOG, 'r') as fd:
            [COMPLETED_MEETING_IDS.add(line.strip()) for line in fd]

    except FileNotFoundError:
        print(
            f"{Color.DARK_CYAN}Log file not found. Creating new log file: {Color.END}"
            f"{COMPLETED_MEETING_IDS_LOG}\n"
        )


def handle_graceful_shutdown(signal_received, frame):
    print(f"\n{Color.DARK_CYAN}SIGINT or CTRL-C detected. system.exiting gracefully.{Color.END}")

    system.exit(0)


# ################################################################
# #                        MAIN                                  #
# ################################################################

def main():
    # clear the screen buffer
    os.system('cls' if os.name == 'nt' else 'clear')
    df_recordings = pd.DataFrame()
    TIME_LIMIT = 55 * 60

    # Get the start time
    start_time = time.time()


    # show the logo
    print(f"""
        {Color.RED}


MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNKXMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWxoXMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMOo0MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMKoOWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMXoxWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNddNMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNxllllo0MMMMMMMMMMWxoX0ollllxNMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWd.    ;KMMMMMMMMMOo0K;    .dWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNl.    cNMMMMMMMKlOXc    .oNMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMX:    .oWMMMMMXoxNo.    cXMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMK;    .kWMMNKddNx.    ;KMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWO'    '0MWx;oXO'    '0MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWx.    ;KOodKK;    .kMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNo.   .;lOWXc    .dWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMXc    .xNXo.   .oNMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMK;  .oNx,.    cXMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMO' cXO.     ;KMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWklKK,     '0MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWWXc     .kWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNl.    .dWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWxc,   .oNMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWOl00, .cXMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM0oOWWkoxKMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMXoxWMMWWWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMNddNMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWxoXMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWkoKMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM0oOWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMXKWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM
MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM

                        Zoom Recording Downloader VIZARDX EDITION

                            Version {APP_VERSION}

        {Color.END}
    """)

    load_access_token()

    load_completed_meeting_ids()

    print(f"{Color.BOLD}Getting user accounts...{Color.END}")
    users = get_users()

    for email, user_id, first_name, last_name in users:
        userInfo = (
            f"{first_name} {last_name} - {email}" if first_name and last_name else f"{email}"
        )
        print(f"\n{Color.BOLD}Getting recording list for {userInfo}{Color.END}")

        recordings = list_recordings(user_id)
        total_count = len(recordings)
        print(f"==> Found {total_count} recordings")

        for recording in recordings:
            df_recording = pd.DataFrame(recording)
            df_recordings = pd.concat([df_recordings, df_recording])

        df_recordings.to_csv('descargas.csv', index=False)

        for index, recording in enumerate(recordings):
            success = False
            meeting_id = recording["uuid"]
            if meeting_id in COMPLETED_MEETING_IDS:
                print(f"==> Skipping already downloaded meeting: {meeting_id}")

                continue

            try:
                downloads = get_downloads(recording)
            except Exception:
                print(
                    f"{Color.RED}### Recording files missing for call with id {Color.END}"
                    f"'{recording['id']}'\n"
                )

                continue

            for file_type, file_extension, download_url, recording_type, recording_id in downloads:
                if recording_type != 'incomplete':
                    filename, folder_name = (
                        format_filename({
                            "file_type": file_type,
                            "recording": recording,
                            "file_extension": file_extension,
                            "recording_type": recording_type,
                            "recording_id": recording_id
                        })
                    )

                    # truncate URL to 64 characters
                    truncated_url = download_url[0:64] + "..."
                    print(
                        f"==> Downloading ({index + 1} of {total_count}) as {recording_type}: "
                        f"{recording_id}: {truncated_url}"
                    )
                    success |= download_recording(download_url, email, filename, folder_name)

                else:
                    print(
                        f"{Color.RED}### Incomplete Recording ({index + 1} of {total_count}) for "
                        f"recording with id {Color.END}'{recording_id}'"
                    )
                    success = False

            if success:
                # if successful, write the ID of this recording to the completed file
                with open(COMPLETED_MEETING_IDS_LOG, 'a') as log:
                    COMPLETED_MEETING_IDS.add(meeting_id)
                    log.write(meeting_id)
                    log.write('\n')
                    log.flush()

    print(f"\n{Color.BOLD}{Color.GREEN}*** All done! ***{Color.END}")
    save_location = os.path.abspath(DOWNLOAD_DIRECTORY)
    print(
        f"\n{Color.BLUE}Recordings have been saved to: {Color.UNDERLINE}{save_location}"
        f"{Color.END}\n"
    )


if __name__ == "__main__":
    # tell Python to shutdown gracefully when SIGINT is received
    signal.signal(signal.SIGINT, handle_graceful_shutdown)

    main()
