#!/usr/bin/env -S python3 -u
#-u to unbuffer output. Otherwise when calling with nohup or redirecting output things are printed very lately or would even mixup

readme="""

Please read README.md in this folder. Latest version is available at https://github.com/JsBergbau/MiTemperature2#readme
This file explains very detailed about the usage and covers everything you need to know as user.

"""

dSENSORS = {
	"A4:C1:38:E2:19:A3" : "ATC_0"
,	"A4:C1:38:B3:AA:91" : "ATC_1"
,	"A4:C1:38:A2:9D:7E" : "ATC_2"
,	"A4:C1:38:50:02:79" : "ATC_3"
,	"A4:C1:38:67:F1:1B" : "ATC_4"
,	"A4:C1:38:23:94:C8" : "ATC_5"
}
dROOMS = {
	"ATC_0" : ""
,	"ATC_1" : "ch parents"
,	"ATC_2" : "ch cedric"
,	"ATC_3" : "ch eva"
,	"ATC_4" : "salon"
,	"ATC_5" : ""
}

from bluepy import btle
import argparse
import os
import re
from dataclasses import dataclass
from collections import deque
import threading
import time
import signal
import traceback
import logging
import requests


@dataclass
class Measurement:
	temperature: float
	humidity: int
	voltage: float
	calibratedHumidity: int = 0
	battery: int = 0
	timestamp: int = 0
	sensorname: str	= ""
	rssi: int = 0 

	def __eq__(self, other): #rssi may be different, so exclude it from comparison
		if self.temperature == other.temperature and self.humidity == other.humidity and self.calibratedHumidity == other.calibratedHumidity and self.battery == other.battery and self.sensorname == other.sensorname:
			#in passive mode also exclude voltage as it changes often due to frequent measurements
			return True #if args.passive else (self.voltage == other.voltage)
		else:
			return False

cnt = 1
measurements=deque()
#globalBatteryLevel=0
previousMeasurements={}
previousCallbacks={}
identicalCounters={}
receiver=None
subtopics=None

def signal_handler(sig, frame):
	disable_le_scan(sock)	
	os._exit(0)
		
def watchDog_Thread():
	global unconnectedTime
	global connected
	global pid
	while True:
		logging.debug("watchdog_Thread")
		logging.debug("unconnectedTime : " + str(unconnectedTime))
		logging.debug("connected : " + str(connected))
		logging.debug("pid : " + str(pid))
		now = int(time.time())
		if (unconnectedTime is not None) and ((now - unconnectedTime) > 60): #could also check connected is False, but this is more fault proof
			pstree=os.popen("pstree -p " + str(pid)).read() #we want to kill only bluepy from our own process tree, because other python scripts have there own bluepy-helper process
			logging.debug("PSTree: " + pstree)
			try:
				bluepypid=re.findall(r'bluepy-helper\((.*)\)',pstree)[0] #Store the bluepypid, to kill it later
			except IndexError: #Should not happen since we're now connected
				logging.debug("Couldn't find pid of bluepy-helper")
			os.system("kill " + bluepypid)
			logging.debug("Killed bluepy with pid: " + str(bluepypid))
			unconnectedTime = now #reset unconnectedTime to prevent multiple killings in a row
		time.sleep(5)
	

def thread_SendingData():
	global previousMeasurements
	global previousCallbacks
	global measurements
	path = os.path.dirname(os.path.abspath(__file__))

	while True:
		try:
			mea = measurements.popleft()
			invokeCallback = True

			if mea.sensorname in previousCallbacks:
				if args.callback_interval > 0 and (int(time.time()) - previousCallbacks[mea.sensorname] < args.callback_interval):
					print("Callback for " + mea.sensorname + " would be within interval (" + str(int(time.time()) - previousCallbacks[mea.sensorname]) + " < " + str(args.callback_interval) + "); don't invoke callback\n")
					invokeCallback = False

			if mea.sensorname in previousMeasurements:
				prev = previousMeasurements[mea.sensorname]
				if mea == prev: #only send data when it has changed
					print("Measurements for " + mea.sensorname + " are identical; don't send data\n")
					identicalCounters[mea.sensorname]+=1
					invokeCallback = False

			if invokeCallback:

				if args.callback:
					fmt = "sensorname,temperature,humidity,voltage" #don't try to separate by semicolon ';' os.system will use that as command separator
					if ' ' in mea.sensorname:
						sensorname = '"' + mea.sensorname + '"'
					else:
						sensorname = mea.sensorname
					params = sensorname + " " + str(mea.temperature) + " " + str(mea.humidity) + " " + str(mea.voltage)
					fmt +=",batteryLevel"
					params += " " + str(mea.battery)
					fmt +=",rssi"
					params += " " + str(mea.rssi)
					params += " " + str(mea.timestamp)
					fmt +=",timestamp"
					cmd = path + "/" + args.callback + " " + fmt + " " + params
					print(cmd)
					ret = os.system(cmd)

				if ret != 0:
					measurements.appendleft(mea) #put the measurement back
					print ("Data couln't be send to Callback, retrying...")
					time.sleep(5) #wait before trying again
				else: #data was sent
					previousMeasurements[mea.sensorname]=Measurement(mea.temperature,mea.humidity,mea.voltage,mea.calibratedHumidity,mea.battery,mea.timestamp,mea.sensorname) #using copy or deepcopy requires implementation in the class definition
					identicalCounters[mea.sensorname]=0
					previousCallbacks[mea.sensorname]=int(time.time())

		except IndexError:
			#print("No Data")
			time.sleep(1)
		except Exception as e:
			print(e)
			print(traceback.format_exc())

sock = None #from ATC 
lastBLEPacketReceived = 0
BLERestartCounter = 1
def keepingLEScanRunning(): #LE-Scanning gets disabled sometimes, especially if you have a lot of BLE connections, this thread periodically enables BLE scanning again
	global BLERestartCounter
	while True:
		time.sleep(1)
		now = time.time()
		if now - lastBLEPacketReceived > args.watchdogtimer:
			print("Watchdog: Did not receive any BLE packet within", int(now - lastBLEPacketReceived), "s. Restarting BLE scan. Count:", BLERestartCounter)
			disable_le_scan(sock)
			enable_le_scan(sock, filter_duplicates=False)
			BLERestartCounter += 1
			print("")
			time.sleep(5) #give some time to take effect

def buildJSONString(measurement):
	jsonstr = '{"temperature": ' + str(measurement.temperature) + ', "humidity": ' + str(measurement.humidity) + ', "voltage": ' + str(measurement.voltage) \
		+ ', "calibratedHumidity": ' + str(measurement.calibratedHumidity) + ', "battery": ' + str(measurement.battery) \
		+ ', "timestamp": '+ str(measurement.timestamp) +', "sensor": "' + measurement.sensorname + '", "rssi": ' + str(measurement.rssi) \
		+ ', "receiver": "' + receiver  + '"}'
	return jsonstr

# Main loop --------
parser=argparse.ArgumentParser(allow_abbrev=False,epilog=readme)
parser.add_argument("--count","-c", help="Read/Receive N measurements and then exit script", metavar='N', type=int)

callbackgroup = parser.add_argument_group("Callback related arguments")
callbackgroup.add_argument("--callback","-call", help="Pass the path to a program/script that will be called on each new measurement")
callbackgroup.add_argument("--callback-interval","-int", help="Only invoke callbackfunction every N seconds, e.g. 600 = 10 minutes",type=int, default=0)

passivegroup = parser.add_argument_group("Passive mode related arguments")
passivegroup.add_argument("--watchdogtimer","-wdt",metavar='X', type=int, help="Re-enable scanning after not receiving any BLE packet after X seconds")


args=parser.parse_args()

if args.callback:
	dataThread = threading.Thread(target=thread_SendingData)
	dataThread.start()

signal.signal(signal.SIGINT, signal_handler)	


if __name__ == "__main__":
	#print("Script started in passive mode")
	#print("------------------------------")
	#print("In this mode all devices within reach are read out, unless a devicelistfile and --onlydevicelist is specified.")
	#print("Also --name Argument is ignored, if you require names, please use --devicelistfile.")
	#print("In this mode debouncing is not available. Rounding option will round humidity and temperature to one decimal place.")
	#print("Passive mode usually requires root rights. If you want to use it with normal user rights, \nplease execute \"sudo setcap cap_net_raw,cap_net_admin+eip $(eval readlink -f `which python3`)\"")
	#print("You have to redo this step if you upgrade your python version.")
	#print("----------------------------")

	import sys
	import bluetooth._bluetooth as bluez
	import cryptoFunctions

	from bluetooth_utils import (toggle_device,
								enable_le_scan, parse_le_advertising_events,
								disable_le_scan, raw_packet_to_str)

	advCounter=dict()
	#encryptedPacketStore=dict()
	sensors = dict()

	dev_id = 0  # the bluetooth device is hci0
	toggle_device(dev_id, True)
	
	try:
		sock = bluez.hci_open_dev(dev_id)
	except:
		print("Error: cannot open bluetooth device %i" % dev_id)
		raise

	enable_le_scan(sock, filter_duplicates=False)

	try:
		prev_data = None

		def decode_data_atc(mac, adv_type, data_str, rssi, measurement):
			preeamble = "161a18"
			packetStart = data_str.find(preeamble)
			offset = packetStart + len(preeamble)
			strippedData_str = data_str[offset:offset+26] #if shorter will just be shorter then 13 Bytes
			strippedData_str = data_str[offset:] #if shorter will just be shorter then 13 Bytes
			macStr = mac.replace(":","").upper()
			dataIdentifier = data_str[(offset-4):offset].upper()

			batteryVoltage=None

			if(dataIdentifier == "1A18") and (len(strippedData_str) in (16, 22, 26, 30)): #only Data from ATC devices
				if len(strippedData_str) == 30: #custom format, next-to-last ist adv number
					advNumber = strippedData_str[-4:-2]
				else:
					advNumber = strippedData_str[-2:] #last data in packet is adv number
				if macStr in advCounter:
					lastAdvNumber = advCounter[macStr]
				else:
					lastAdvNumber = None
				if lastAdvNumber == None or lastAdvNumber != advNumber:

					if len(strippedData_str) == 26: #ATC1441 Format
						print("BLE packet - ATC1441: %s %02x %s %d" % (mac, adv_type, data_str, rssi))
						name = dSENSORS[mac] if mac in dSENSORS.keys() else ""
						room = dROOMS[name] if name in dROOMS.keys() else ""
						print(f"name:{name} room:{room}")
						advCounter[macStr] = advNumber
						#temperature = int(data_str[12:16],16) / 10.    # this method fails for negative temperatures
						temperature = int.from_bytes(bytearray.fromhex(strippedData_str[12:16]),byteorder='big',signed=True) / 10.
						humidity = int(strippedData_str[16:18], 16)
						batteryVoltage = int(strippedData_str[20:24], 16) / 1000
						batteryPercent = int(strippedData_str[18:20], 16)

					else: #no fitting packet
						return

				else: #Packet is just repeated
					return

				measurement.battery = batteryPercent
				measurement.humidity = humidity
				measurement.temperature = temperature
				measurement.voltage = batteryVoltage if batteryVoltage != None else 0
				measurement.rssi = rssi
				return measurement


		def le_advertise_packet_handler(mac, adv_type, data, rssi):
			global lastBLEPacketReceived
			if args.watchdogtimer:
				lastBLEPacketReceived = time.time()
			lastBLEPacketReceived = time.time()
			data_str = raw_packet_to_str(data)

			global measurements
			measurement = Measurement(0,0,0,0,0,0,0,0)
			measurement = (
				decode_data_atc(mac, adv_type, data_str, rssi, measurement)
			)

			if measurement:
				measurement.timestamp = int(time.time())

				print("Temperature: ", measurement.temperature)
				print("Humidity: ", measurement.humidity)
				if measurement.voltage != None:
					print ("Battery voltage:", measurement.voltage,"V")
				print("RSSI:", rssi, "dBm")
				print("Battery:", measurement.battery,"%")
				
				if args.callback:
					measurements.append(measurement)

				global cnt
				print(f"cnt:{cnt}/{args.count}")
				cnt += 1
				if args.count and cnt > args.count:
					sys.exit(0)
				print("")

		if  args.watchdogtimer:
			keepingLEScanRunningThread = threading.Thread(target=keepingLEScanRunning)
			keepingLEScanRunningThread.start()
			logging.debug("keepingLEScanRunningThread started")

		# Blocking call (the given handler will be called each time a new LE
		# advertisement packet is detected)
		parse_le_advertising_events(sock, handler=le_advertise_packet_handler, debug=False)

	except KeyboardInterrupt:
		disable_le_scan(sock)
