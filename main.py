import network
import config
import ulogging
import time, utime
from machine import reset
import ubinascii
import json
import uwebsockets.client
import hardware
import utils
import gc

if config.ENABLE_BACKUP_HTTP_SERVER:
    import httpserver

ulogging.basicConfig(level=config.LOG_LEVEL)
logger = ulogging.getLogger("main")

hardware.buzzer_off()
hardware.rgb_led_set(hardware.RGB_OFF)
hardware.led_off()

# setup is starting
hardware.rgb_led_set(hardware.RGB_PURPLE)
hardware.alert(rgb_return_colour=hardware.RGB_PURPLE)
hardware.lcd.print("Initializing...")


INTERLOCK_SESSION = {
    "session_id": None,  # any value == on, None == off
    "session_kwh": 0,
}
STATE = {"locked_out": False, "tag_hash": ""}
authorised_rfid_tags = []

sta_if = network.WLAN(network.STA_IF)
local_ip = None  # store our local IP address
local_mac = ubinascii.hexlify(sta_if.config("mac")).decode()  # store our mac address
hostname = "BeepBeep_" + local_mac

logger.info("Setting hostname to: " + hostname)
network.hostname(hostname)
logger.info("Setting WiFi country code to: " + config.WIFI_COUNTRY_CODE)
network.country(config.WIFI_COUNTRY_CODE)

last_card_id = None
websocket = None
led_update = time.ticks_ms()
ten_second_cron_update = time.ticks_ms()
last_pong = None
last_rfid_sync = time.ticks_ms()
waiting_for_door_open_time = None
door_opened_time = None


# setup RFID
if config.WIEGAND_ENABLED:
    # setup wiegand reader
    import uwiegand

    rfid_reader = uwiegand.Wiegand(
        config.WIEGAND_ZERO,
        config.WIEGAND_ONE,
        uid_32bit_mode=config.UID_32BIT_MODE,
        timer_id=config.WIEGAND_TIMER_ID,
        faculty_and_uid_combo=config.FACILITY_AND_UID_COMBO,
    )
else:
    from urdm6300.urdm6300 import Rdm6300

    rfid_reader = Rdm6300(rx=config.UART_RX_PIN, tx=config.UART_TX_PIN)

try:
    # create it if it doesn't exist
    if not utils.file_or_dir_exists("tags.json"):
        with open("tags.json", "w") as new_tags:
            json.dump([], new_tags)

        # if we have any saved tags, load them
    with open("tags.json") as tags:
        parsed_tags = json.load(tags)
        if parsed_tags:
            authorised_rfid_tags = parsed_tags
            logger.info("Loaded %s saved tags from flash.", len(authorised_rfid_tags))
except Exception as e:
    logger.error("Could not load saved tags (unhandled error)")
    logger.error(e)


def save_state(state):
    global STATE
    logger.info("Saving STATE!!")

    STATE = state  # update the in memory state

    try:
        # save the state to flash
        with open("state.json", "w") as state_file:
            json.dump(state, state_file)
            logger.debug("saved state to flash")
        return True

    except Exception as e:
        logger.error("Saving STATE FAILED! Exception:")
        logger.error(str(e))
        return False


def get_state():
    global STATE
    try:
        # create it if it doesn't exist
        if not utils.file_or_dir_exists("state.json"):
            with open("state.json", "w") as state_file:
                json.dump(STATE, state_file)

        with open("state.json") as state_file:
            STATE = json.load(state_file)
            if STATE:
                logger.info("Loaded saved STATE from flash.")
            return STATE

    except Exception as e:
        logger.error("Could not load saved STATE (unhandled error)")
        logger.error(str(e))
        return False


def connect_wifi(silent=False):
    global local_ip

    wlan_connecting_start = time.ticks_ms()
    led_toggle_last_update = time.ticks_ms()
    led_toggle_last_state = False

    if sta_if.isconnected():
        sta_if.disconnect()
        time.sleep(0.5)

    sta_if.active(False)
    time.sleep(0.5)
    sta_if.active(True)
    if getattr(config, "USE_STATIC_IP", False):
        try:
            sta_if.ifconfig(
                (
                    config.IP_ADDRESS,
                    config.SUBNET_MASK,
                    config.GATEWAY,
                    config.DNS_SERVER,
                )
            )
            logger.info("Using static IP config: " + config.IP_ADDRESS)
        except Exception as e:
            logger.error("Failed to set static IP config, falling back to DHCP")
            logger.error(e)
    # sta_if.config(pm=sta_if.PM_NONE)  # disable power management
    # sta_if.config(reconnects=-1)
    if config.WIFI_TX_POWER:
        sta_if.config(txpower=config.WIFI_TX_POWER)

    logger.info("Connecting To WiFi...")
    if not silent:
        hardware.lcd.clear()
        hardware.lcd.print("Connecting WiFi")
        hardware.status_led_off()
    try:
        sta_if.connect(config.WIFI_SSID, config.WIFI_PASS)
    except OSError as e:
        logger.error(e)

    if not silent:
        while not sta_if.isconnected():
            time.sleep_ms(100)
            hardware.feedWDT()
            if time.ticks_diff(time.ticks_ms(), wlan_connecting_start) > 10000:
                logger.warn("Took too long to wait for WiFi!")
                logger.warn(
                    "The ESP32 should continue trying to connect in the background."
                )
                try:
                    sta_if.connect(config.WIFI_SSID, config.WIFI_PASS)
                except OSError as e:
                    logger.error(e)
                hardware.led_off()
                hardware.status_led_off()
                hardware.rgb_led_set(hardware.RGB_PURPLE)  # booting up colour
                return False

            if time.ticks_diff(time.ticks_ms(), led_toggle_last_update) > 250:
                led_toggle_last_update = time.ticks_ms()

                if led_toggle_last_state:
                    hardware.led_off()
                    hardware.status_led_off()
                    hardware.rgb_led_set(hardware.RGB_OFF)
                    led_toggle_last_state = False

                else:
                    hardware.led_on()
                    hardware.status_led_on()
                    hardware.rgb_led_set(hardware.RGB_PURPLE)
                    led_toggle_last_state = True

        hardware.led_off()
        hardware.rgb_led_set(hardware.RGB_PURPLE)  # booting up colour

    if sta_if.isconnected():
        hardware.status_led_on()
        new_ip = sta_if.ifconfig()[0]
        if new_ip != local_ip:
            local_ip = new_ip
            logger.info("New Local IP: " + local_ip)
        return True
    else:
        hardware.status_led_off()
        return False


def connect_websocket():
    global websocket, last_pong

    if not sta_if.isconnected():
        logger.warn("Tried to setup websocket but WiFi is not connected...")
        connect_wifi(silent=True)
        return

    WS_URL = f"{config.PORTAL_WS_URL}/{config.DEVICE_TYPE}/{local_mac}"

    try:
        logger.info("Connecting to websocket...")
        logger.debug("WS_URL: " + WS_URL)
        hardware.status_led_off()
        hardware.lcd.clear()
        hardware.lcd.print("Connecting WS")
        websocket = uwebsockets.client.connect(WS_URL)
        last_pong = time.ticks_ms()

        auth_packet = {
            "command": "authenticate",
            "secret_key": config.API_SECRET,
        }
        websocket.send(json.dumps(auth_packet))

        ip_packet = {"command": "ip_address", "ip_address": local_ip}
        websocket.send(json.dumps(ip_packet))
        hardware.status_led_on()

    except Exception as e:
        logger.error("Couldn't connect to websocket!")
        logger.error(e)
        hardware.lcd.clear()
        hardware.lcd.print("WS Connect Fail")
        hardware.status_led_off()


def save_tags(new_tags):
    global authorised_rfid_tags
    logger.info("Syncing tags!!")

    try:
        authorised_rfid_tags = new_tags
        logger.info("Got %s tags!", len(authorised_rfid_tags))
        # save the tags to flash
        with open("tags.json", "w") as tags_file:
            json.dump(authorised_rfid_tags, tags_file)
            logger.debug("Saved tags to flash")

        logger.debug("Syncing tags done!")

    except Exception as e:
        logger.error("Syncing tags FAILED! Exception:")
        logger.error(str(e))


def log_door_swipe(card_id, rejected=False, locked_out=False):
    success_string = "failed" if rejected or locked_out else "successful"
    logger.info(f"Logging {success_string} door swipe!")

    if websocket:
        try:
            if rejected:
                websocket.send(
                    json.dumps({"command": "log_access_denied", "card_id": card_id})
                )
            elif locked_out:
                websocket.send(
                    json.dumps({"command": "log_access_locked_out", "card_id": card_id})
                )
            else:
                websocket.send(
                    json.dumps({"command": "log_access", "card_id": card_id})
                )
        except Exception as e:
            logger.warn(f"Exception when logging {success_string} access!")
            logger.error(e)

def lcd_interlock_unlock_success():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("   Authorized!  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print("  Device Active ")
    hardware.lcd.backlight()

def lcd_interlock_unlock_fail():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print(" Unauthorized!  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print(" Contact MNGMT! ")
    hardware.lcd.backlight()

def lcd_interlock_timeout_warning(seconds):
    # hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print(" Device Active  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print(f" Off in {seconds} Secs ")
    # hardware.lcd.backlight()
    
def lcd_door_access_success():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("   Authorized!  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print(" Door Unlocked! ")
    hardware.lcd.backlight()

def lcd_door_access_fail():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("   Locked Out!  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print(" Contact MNGMT! ")
    hardware.lcd.backlight()

def lcd_interlock_standby_msg():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("   Interlock    ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print("Swipe To Unlock!")
    hardware.lcd.backlight()

def lcd_door_access_standby_msg():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("  Door Locked!  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print("Swipe To Unlock!")
    hardware.lcd.backlight()

def lcd_interlock_session_end():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("  Session End!  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print(" Interlock Off  ")
    hardware.lcd.backlight()

def lcd_device_locked_out():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("     Device     ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print("  Locked Out!   ")
    hardware.lcd.backlight()

def lcd_door_access_bump_door():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("  Bumping Door  ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print("  In 5 Seconds  ")
    hardware.lcd.backlight()

def lcd_access_door_open():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("    Warning!    ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print("   Close Door   ")
    hardware.lcd.backlight()

def lcd_rfid_tag_unknown():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print(" Unknown Card   ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print(" Contact MNGMT! ")
    hardware.lcd.backlight()


def lcd_fob_tester_standby_msg():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0, 0)
    hardware.lcd.print("   Fob Tester   ")
    hardware.lcd.set_cursor(0, 1)
    hardware.lcd.print("  Swipe A Card  ")
    hardware.lcd.backlight()


def lcd_fob_tester_lookup_pending():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0, 0)
    hardware.lcd.print(" Looking Up Card")
    hardware.lcd.set_cursor(0, 1)
    hardware.lcd.print(" Please Wait... ")
    hardware.lcd.backlight()


def lcd_fob_tester_no_connection():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0, 0)
    hardware.lcd.print("    Offline     ")
    hardware.lcd.set_cursor(0, 1)
    hardware.lcd.print("   No Lookup    ")
    hardware.lcd.backlight()


def lcd_fob_tester_unknown_card():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0, 0)
    hardware.lcd.print("  Card Unknown  ")
    hardware.lcd.set_cursor(0, 1)
    hardware.lcd.print("  Not Assigned  ")
    hardware.lcd.backlight()


def _lcd_line(text):
    formatted = str(text or "")[:16]
    return formatted + " " * (16 - len(formatted))


def lcd_fob_tester_lookup_result(full_name, account_status):
    status_map = {
        "noob": "Need Induction",
        "active": "Active",
        "inactive": "Inactive",
        "accountonly": "Account Only",
    }

    hardware.lcd.clear()
    hardware.lcd.set_cursor(0, 0)
    hardware.lcd.print(_lcd_line(full_name))
    hardware.lcd.set_cursor(0, 1)
    hardware.lcd.print(_lcd_line(status_map.get(account_status, "Status Hidden")))
    hardware.lcd.backlight()

def lcd_device_reboot():
    hardware.lcd.clear()
    hardware.lcd.set_cursor(0,0)
    hardware.lcd.print("     Device     ")
    hardware.lcd.set_cursor(0,1)
    hardware.lcd.print("     Reboot     ")
    hardware.lcd.backlight()

def interlock_end_session():
    if (
        INTERLOCK_SESSION.get("session_id")
        and INTERLOCK_SESSION.get("session_id") != "system"
    ):
        print(INTERLOCK_SESSION.get("session_id"))
        try:
            interlock_packet = {
                "command": "interlock_session_end",
                "session_id": INTERLOCK_SESSION.get("session_id"),
                "session_kwh": INTERLOCK_SESSION.get("session_kwh"),
                "card_id": card,
            }
            websocket.send(json.dumps(interlock_packet))
        except Exception as e:
            hardware.alert()
            logger.error("Failed to end interlock session!")
            logger.error(e)

    INTERLOCK_SESSION["session_id"] = None
    INTERLOCK_SESSION["session_kwh"] = 0
    hardware.interlock_power_control(False)
    hardware.interlock_session_ended()
    lcd_interlock_session_end()
    time.sleep(2.0)
    hardware.reset_interlock_power_usage()
    print_device_standby_message()


def handle_swipe_door(card: str):
    global authorised_rfid_tags
    hardware.buzz_card_read()

    if card in authorised_rfid_tags:
        if STATE["locked_out"]:
            log_door_swipe(card, locked_out=True)
            hardware.alert()

        else:
            log_door_swipe(card)
            unlock_door()

    else:
        log_door_swipe(card, rejected=True)
        lcd_door_access_fail()
        hardware.alert()


def handle_swipe_interlock(card: str):
    # request a new interlock session
    if INTERLOCK_SESSION.get("session_id") is None:
        interlock_packet = {
            "command": "interlock_session_start",
            "card_id": card,
        }
        hardware.reset_interlock_power_usage()
        lcd_interlock_unlock_success()
        time.sleep(1.0)
        try:
            websocket.send(json.dumps(interlock_packet))
        except Exception as e:
            logger.error("Failed to start interlock session!")
            logger.error(e)
            hardware.alert()

    # turn off the interlock if it was manually turned on by the system
    elif INTERLOCK_SESSION.get("session_id") == "system":
        interlock_packet = {"command": "interlock_off"}

        try:
            websocket.send(json.dumps(interlock_packet))
            interlock_end_session()
            print_device_standby_message
        except Exception as e:
            logger.error("Failed to turn off interlock!")
            logger.error(e)
            hardware.alert()

    # end the current interlock session
    else:
        interlock_end_session()
        print_device_standby_message()


def handle_swipe_memberbucks(card_id: str):
    # attempt to debit the card
    debit_packet = {
        "command": "debit",
        "card_id": card_id,
        "amount": config.VEND_PRICE / 100,
    }
    try:
        websocket.send(json.dumps(debit_packet))
        hardware.lcd.clear()
        hardware.lcd.print("Please Wait... ")
        hardware.lcd.blink()
    except Exception as e:
        logger.error("Failed to send debit packet!")
        logger.error(e)
        hardware.alert()


def handle_swipe_fob_tester(card_id: str):
    if not sta_if.isconnected() or not (websocket and websocket.open):
        lcd_fob_tester_no_connection()
        hardware.alert()
        return

    lookup_packet = {
        "command": "fob_lookup",
        "card_id": card_id,
        "request_id": str(time.ticks_ms()),
    }

    try:
        websocket.send(json.dumps(lookup_packet))
        lcd_fob_tester_lookup_pending()
    except Exception as e:
        logger.error("Failed to send fob lookup packet!")
        logger.error(e)
        lcd_fob_tester_no_connection()
        hardware.alert()


def print_device_standby_message():
    if config.DEVICE_TYPE == "door":
        lcd_door_access_standby_msg()
        
    elif config.DEVICE_TYPE == "memberbucks" or config.DEVICE_TYPE == "interlock":
        if sta_if.isconnected():
            if config.DEVICE_TYPE == "memberbucks":
                hardware.lcd.print(f"${config.VEND_PRICE/100} Swipe Card")
            else:
                lcd_interlock_standby_msg()

        else:
            hardware.lcd.print(f"No Connection")

    elif config.DEVICE_TYPE == "fobtester":
        if sta_if.isconnected() and websocket and websocket.open:
            lcd_fob_tester_standby_msg()
        else:
            lcd_fob_tester_no_connection()


def unlock_door():
    global waiting_for_door_open_time

    hardware.unlock()
    hardware.relay_on()
    logger.warn("Unlocked!")
    hardware.rgb_led_set(hardware.RGB_GREEN)
    lcd_door_access_success()
    hardware.buzz_ok(flash_led=False)

    if config.DOOR_SENSOR_ENABLED:
        waiting_for_door_open_time = time.ticks_ms()
        print_device_standby_message()
    else:
        time.sleep(config.FIXED_UNLOCK_DELAY)
        lock_door()
        hardware.relay_off()


def lock_door():
    global waiting_for_door_open_time

    waiting_for_door_open_time = None
    hardware.lock()
    hardware.relay_off()
    logger.warn("Locked!")
    print_device_standby_message()


get_state()  # grab the state from the flash
connect_wifi()  # connect to wifi
connect_websocket()  # connect to the websocket

if config.ENABLE_BACKUP_HTTP_SERVER:
    import uselect

    logger.warning(
        "The backup http server is enabled. This is not recommended for production use!"
    )

    # try to set up the http server
    if not httpserver.setup_http_server():
        logger.error("FAILED to setup http server on startup :(")
    else:
        poll = uselect.poll()
        poll.register(httpserver.sock, uselect.POLLIN)
else:
    logger.debug("Backup http server disabled!")

logger.info("Starting main loop...")
hardware.led_on()
time.sleep(0.5)
hardware.led_off()
hardware.rgb_led_set(hardware.RGB_BLUE)
print_device_standby_message()

door_previous_state = hardware.get_door_sensor_state()
in_1_previous_state = hardware.get_in_1_state()
wifi_status_led_toggle = False
wifi_status_led_update = time.ticks_ms()
interlock_timeout_timer = None

hardware.out_1_on()

while True:
    hardware.feedWDT()
    if in_1_previous_state != hardware.get_in_1_state():
        in_1_previous_state = hardware.get_in_1_state()
        logger.info(f"In 1 sensor state changed to {in_1_previous_state}")

    if door_previous_state != hardware.get_door_sensor_state():
        door_previous_state = hardware.get_door_sensor_state()
        logger.info(f"Door sensor state changed to {door_previous_state}")
        if door_previous_state:
            door_opened_time = time.ticks_ms()
        else:
            door_opened_time = None

    if waiting_for_door_open_time:
        if (
            time.ticks_diff(time.ticks_ms(), waiting_for_door_open_time)
            > config.DOOR_SENSOR_TIMEOUT * 1000
        ):
            logger.info("Door sensor timeout! Locking again.")
            lock_door()

        else:
            # if the door has been opened, let's lock it immediately
            if hardware.get_door_sensor_state():
                logger.info("Door opened while waiting, locking in 0.5s.")
                time.sleep(0.5)
                lock_door()

        print_device_standby_message()

    # if the door has been open too long
    if door_opened_time and config.DOOR_OPEN_ALARM_TIMEOUT:
        if (
            time.ticks_diff(time.ticks_ms(), door_opened_time)
            > config.DOOR_OPEN_ALARM_TIMEOUT * 1000
        ):
            logger.warn("Door left open alarm!")
            lcd_access_door_open()
            hardware.buzzer_on()

    try:
        if card := rfid_reader.read_card():
            card = str(card)
            logger.info(f"Got a card: {card}")

            if config.BUZZ_ON_SWIPE:
                hardware.buzz_card_read()

            if config.DEVICE_TYPE == "door":
                handle_swipe_door(card)

            elif config.DEVICE_TYPE == "interlock":
                handle_swipe_interlock(card)

            elif config.DEVICE_TYPE == "memberbucks":
                handle_swipe_memberbucks(card)

            elif config.DEVICE_TYPE == "fobtester":
                handle_swipe_fob_tester(card)

            # dedupe card reads; keep looping until we've cleared the buffer
            # while not rfid_reader.read_card():
            #     pass
            last_card_id = card
            card = None

        # if WiFi still isn't connected, flash the wifi LED
        if not sta_if.isconnected():
            if time.ticks_diff(time.ticks_ms(), wifi_status_led_update) > 250:
                wifi_status_led_update = time.ticks_ms()

                if wifi_status_led_toggle:
                    hardware.status_led_off()
                    wifi_status_led_toggle = False

                else:
                    hardware.status_led_on()
                    wifi_status_led_toggle = True

        # TODO rate limit to 1 second
        if (interlock_timeout_timer is not None):
                        lcd_interlock_timeout_warning(config.INTERLOCK_TIMEOUT_TIME-(time.ticks_diff(time.ticks_ms(), interlock_timeout_timer)//1000))              

        if (
            time.ticks_diff(time.ticks_ms(), ten_second_cron_update)
            > config.CRON_PERIOD
        ):
            ten_second_cron_update = time.ticks_ms()
            gc.collect()

            # if we've missed at least 3 consecutive pongs, then reconnect
            if time.ticks_diff(time.ticks_ms(), last_pong) > config.CRON_PERIOD * 3:
                websocket = None
                logger.info("Websocket not open (pong timeout), trying to reconnect.")
                

                # this stops us trying to reconnect every 10 seconds and holding up the main loop
                last_pong = time.ticks_ms()

            if websocket and websocket.open:
                try:
                    logger.debug("sending ping")
                    websocket.send(json.dumps({"command": "ping"}))
                    hardware.status_led_on()
                except Exception as e:
                    websocket = None
                    logger.error("Websocket not open, trying to reconnect.")
                    logger.error(e)
                    hardware.status_led_off()
                    continue

                if (INTERLOCK_SESSION.get("session_id") is not None
                        and INTERLOCK_SESSION.get("session_id") != "system"):

                    INTERLOCK_SESSION["session_kwh"] = (hardware.get_interlock_power_usage())
                    INTERLOCK_SESSION["active_current"] = (hardware.get_interlock_current_draw())
                    logger.debug("Sending interlock session update")
                    interlock_packet = {
                        "command": "interlock_session_update",
                        "session_id": INTERLOCK_SESSION.get("session_id"),
                        "session_kwh": INTERLOCK_SESSION.get("session_kwh"),
                    }
                    
                    try:
                        websocket.send(json.dumps(interlock_packet))
                    except Exception as e:
                        logger.error("Failed to send interlock session update!")
                        logger.error(e)
                        websocket = None
                        continue
                    
                    logger.debug(f"Timeout - {interlock_timeout_timer}")
                    if (interlock_timeout_timer is None and INTERLOCK_SESSION.get("active_current")<config.INTERLOCK_TIMEOUT_CURRENT):
                        interlock_timeout_timer = time.ticks_ms()
                        hardware.lcd.clear()
                        lcd_interlock_timeout_warning(config.INTERLOCK_TIMEOUT_TIME)
                        logger.error("int cound down start")
  
                    if (INTERLOCK_SESSION.get("active_current")>config.INTERLOCK_TIMEOUT_CURRENT):
                        interlock_timeout_timer = None
                        lcd_interlock_unlock_success()

                    if (interlock_timeout_timer is not None and time.ticks_diff(time.ticks_ms(), interlock_timeout_timer)>config.INTERLOCK_TIMEOUT_TIME*1000):
                        interlock_timeout_timer = None
                        logger.error("int end")
                        interlock_end_session()
                        

                                    

            else:
                logger.info("Websocket not open, trying to reconnect.")
                connect_websocket()

            if sta_if.isconnected():
                local_ip = sta_if.ifconfig()[0]  # update our local IP address

        if websocket and websocket.open:
            if data := websocket.recv():
                logger.debug("Got websocket packet:")
                logger.debug(data)

                try:
                    data = json.loads(data)

                    if data.get("authorised") is not None:
                        logger.info("Got authorisation packet.")
                        print_device_standby_message()

                    elif data.get("command") == "pong":
                        last_pong = time.ticks_ms()

                    elif data.get("command") == "ping":
                        websocket.send(json.dumps({"command": "pong"}))

                    elif data.get("command") == "reboot":
                        logger.warn("Rebooting device!")
                        if config.DEVICE_TYPE == "interlock":
                            interlock_end_session()
                            lcd_device_reboot()
                            time.sleep(2.00)
                        else:
                            hardware.lock()
                            hardware.buzz_action()
                        hardware.rgb_led_set(hardware.RGB_OFF)
                        reset()

                    elif data.get("command") == "update_device_locked_out":
                        locked_out = data.get("locked_out")
                        logger.info(f"Updating device locked out {locked_out}!")
                        STATE["locked_out"] = locked_out
                        if locked_out:
                            lcd_device_locked_out()
                        else:
                            lcd_interlock_standby_msg
                        save_state(STATE)

                    elif data.get("command") == "bump" and config.DEVICE_TYPE == "door":
                        logger.info("Bumping Door!")
                        lcd_door_access_bump_door()
                        unlock_door()

                    elif data.get("command") == "sync":
                        tags_hash_new = data.get("hash")
                        tags_hash_current = STATE.get("tag_hash")

                        if tags_hash_new != tags_hash_current:
                            save_tags(data.get("tags"))
                            STATE["tag_hash"] = tags_hash_new
                            save_state(STATE)
                            logger.info(f"Saved tags with hash: {tags_hash_new}")
                        else:
                            logger.info("Tags hash unchanged, skipping save.")

                    elif data.get("command") == "unlock":
                        if config.DEVICE_TYPE == "interlock":
                            logger.info("Turning on interlock from manual request!")
                            if hardware.interlock_power_control(True):
                                INTERLOCK_SESSION["session_id"] = (
                                    "system"  # special state - manually turned on by the system
                                )
                                lcd_interlock_unlock_success()
                                hardware.interlock_session_started()

                            elif config.DEVICE_TYPE == "door":
                                logger.warn("Interlock power control failed!")
                                hardware.alert()
                        else:
                            logger.info("Unlocking device from manual request!")
                            hardware.unlock()

                    elif data.get("command") == "lock":
                        if config.DEVICE_TYPE == "door":
                            logger.info("Locking device from manual request!")
                            hardware.lock()

                        elif config.DEVICE_TYPE == "interlock":
                            logger.info("Turning off interlock from manual request!")
                            lcd_interlock_session_end
                            interlock_end_session()
                    elif data.get("command") == "interlock_session_start":
                        if config.DEVICE_TYPE == "interlock":
                            logger.info("Turning on interlock from new session!")

                            INTERLOCK_SESSION["session_id"] = data.get("session_id")
                            INTERLOCK_SESSION["session_kwh"] = 0

                            if hardware.interlock_power_control(True):
                                hardware.interlock_session_started()
                                

                            else:
                                logger.warn("Interlock power control failed!")
                                hardware.alert()

                    elif data.get("command") == "interlock_session_rejected":
                        if config.DEVICE_TYPE == "interlock":
                            logger.info("Interlock session request failed!")
                            hardware.alert()

                    elif data.get("command") == "interlock_session_update":
                        
                        pass

                    elif data.get("command") == "debit":
                        hardware.lcd.reset_screen()
                        logger.debug(data)
                        success = data.get("success")
                        balance = data.get("balance")

                        balance = (
                            f"${str(round(float(balance) / 100, 2))}"
                            if balance
                            else "Unknown"
                        )
                        if success:
                            logger.info("Debit successful!")
                            hardware.lcd.clear()
                            hardware.lcd.print_rocket()
                            hardware.lcd.print(f"Success! {balance}")
                            hardware.buzz_action()
                            hardware.vend_product()
                            time.sleep(5)
                        else:
                            logger.info("Debit failed!")
                            hardware.lcd.clear()
                            hardware.lcd.print(f"Declined. {balance}")
                            hardware.lcd.no_backlight()
                            hardware.rgb_led_set(hardware.RGB_RED)
                            time.sleep(0.25)
                            hardware.lcd.backlight()
                            time.sleep(0.25)
                            hardware.lcd.no_backlight()
                            time.sleep(0.25)
                            hardware.lcd.backlight()
                            hardware.feedWDT()
                            time.sleep(5)
                            hardware.rgb_led_set(hardware.RGB_BLUE)
                        print_device_standby_message()

                    elif data.get("command") == "fob_lookup_result":
                        if config.DEVICE_TYPE == "fobtester":
                            if data.get("found"):
                                lcd_fob_tester_lookup_result(
                                    data.get("full_name"),
                                    data.get("account_status"),
                                )
                            elif data.get("reason") == "unknown_card":
                                lcd_fob_tester_unknown_card()
                            else:
                                lcd_fob_tester_no_connection()

                            time.sleep(4)
                            print_device_standby_message()
                    else:
                        logger.warn("Unknown websocket packet!")
                        logger.warn(json.dumps(data))

                except Exception as e:
                    logger.error("Error parsing JSON websocket packet!")
                    logger.error(str(e))

        if config.ENABLE_BACKUP_HTTP_SERVER:
            # backup http server for manually bumping a door from the local network
            for _ in poll.poll(1):
                conn, addr = httpserver.sock.accept()
                request = str(conn.recv(2048))
                hardware.rgb_led_set(hardware.RGB_PURPLE)
                logger.info("got http request!")
                logger.info(request)
                time.sleep(0.1)
                hardware.rgb_led_set(hardware.RGB_BLUE)
                httpserver.client_response(conn)
                if f"/bump?secret={config.API_SECRET}" in request:
                    logger.info("got authenticated bump request")
                    hardware.door_swipe_success()
                    break

    except KeyboardInterrupt as e:
        # turn off the LED and buzzer in case they were left on
        hardware.led_off()
        hardware.rgb_led_set(hardware.RGB_WHITE)
        hardware.buzzer_off()
        hardware.status_led_off()
        hardware.lcd.clear()
        hardware.lcd.print("KeybInt Stopped.")

        raise e

    except Exception as e:
        if config.CATCH_ALL_EXCEPTIONS:
            logger.error(
                "excepted, but config.CATCH_ALL_EXCEPTIONS is enabled so ignoring :("
            )
            logger.error(e)
        else:
            logger.error(
                "excepted, but config.CATCH_ALL_EXCEPTIONS is disabled so throwing :o"
            )
            logger.error(e)
            hardware.lcd.clear()
            hardware.lcd.print("Error Stopped.")
            # turn off the LED and buzzer in case they were left on
            hardware.led_off()
            hardware.rgb_led_set(hardware.RGB_WHITE)
            hardware.buzzer_off()
            raise e
