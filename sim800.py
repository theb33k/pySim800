#!/usr/bin/python
# coding: utf-8

from __future__ import print_function
from builtins import input

import logging
logger = logging.getLogger(__name__)
import serial
import time
import re
import os

import RPi.GPIO as GPIO

class Sim800(object):
    def __init__(self, device, resetPin=17, powerSupplyDisablePin=None):
        self.__device = device
        self.__serial = None
        self.__serialBaudrate = 9600
        self.__availableSms = []
        self.__connected = False
        self.__gprsReady = False
        self.__gprsBearerId = None
        self.__ipAddress = None
        # setup reset pin
        self.__resetPin = resetPin
        GPIO.setmode(GPIO.BCM)  
        GPIO.setup(self.__resetPin, GPIO.OUT, pull_up_down=GPIO.PUD_OFF)
        GPIO.output(self.__resetPin, GPIO.HIGH) # reset is active LOW
        # setup powerSuply pin
        self.__psDisablePin = powerSupplyDisablePin
        if self.__psDisablePin is not None:
            GPIO.setup(self.__psDisablePin, GPIO.OUT, pull_up_down=GPIO.PUD_OFF)
            GPIO.output(self.__psDisablePin, GPIO.LOW) # PS is OFF when pin is HIGH
    
    def begin(self, device=None, baudrate=None, timeout=1.5):
        if not self.__connected:
            if device:
                self.__device = device
            if baudrate is not None:
                self.__serialBaudrate = baudrate
            self.__serial = serial.Serial(self.__device, baudrate=self.__serialBaudrate, timeout=timeout)
            # self.__serial.write(b'\x1B') # ESC
            self.__serial.reset_input_buffer()
            if not self.__ping():
                # reset with GPIO then wait for 10 seconds ?
                if not self.__recovery():
                    self.__serial.close()
                    return False
            if self.__setupGSM():
                self.__connected = True
                return True
        else:
            if timeout and timeout != self.__serial.timeout:
                self.__serial.timeout = timeout
            return True
        self.__serial.close()
        return False

    def stop(self):
        if self.__connected:
            self.__serial.close()
            self.__connected = False
        else:
            logger.error("Trying to call stop() while sim800 is not connected")
            return False

    def available(self):
        if self.__connected:
            self.__checkNewSms()
            return len(self.__availableSms)
        else:
            logger.error("Trying to call available() while sim800 is not connected")
            return False

    def readSms(self): 
        """reads the oldest unread sms and delete it from sim800 module"""
        if self.__connected:
            if self.available() > 0:
                i = self.__availableSms[0]
                del self.__availableSms[0]
                tx = "AT+CMGR=%d,0" % i
                self.__write(tx)
                rx = self.__readline()
                if tx in rx: # echo enabled ?
                    rx = self.__readline()
                m = re.search(r'(\+CMGR): [^,]*,"([^"]*)",".*"', rx)
                if m:
                    g = m.groups()
                    resp = g[0]
                    sender = g[1]
                    text = self.__readline()
                    atStatus = self.__checkStatus()
                    if "CMGR" in resp and atStatus:
                        self.__deleteSmsByIndex(i)
                        return {"sender": sender, "text": text}
                    else:
                        logger.error("Invalid response to AT+CMGR")
                else:
                    logger.error("readSms regex did not match CMGR response: %s", rx)
            return None
        else:
            logger.error("Trying to call readSms() while sim800 is not connected")
            return False
    
    def sendSms(self, number, text):
        """send sms and delete it from sim800 module"""
        if self.__connected:
            logger.debug("Send SMS to %s", number)
            if self.__setTextMode():
                self.__write('AT+CMGS="%s"' % number)
                response = self.__readline(5)
                if ">" in response or "AT+CMGS" in response:
                    self.__serial.reset_input_buffer()
                    self.__write(text, end="")
                    self.__serial.write(b'\x1A') # CTRL+Z
                    if self.__checkStatus(timeout=10):
                        logger.debug("SMS sent")
                        return True
                    else:
                        logger.error("Unable to send SMS")
                else:
                    self.__write("\x1b", end="") # ESC to cancel SMS sending
                    logger.error("Failed to setup SMS sending")
            else:
                logger.error("Unable to set Text Mode")
            return False
        else:
            logger.error("Trying to call sendSms() while sim800 is not connected")
            return False

    def flush(self):
        if self.__connected:
            return self.__deleteSms("ALL")
        else:
            logger.error("Trying to call flush() while sim800 is not connected")
            return False

    def isOpen(self):
        return self.__connected == True

    def setupGPRS(self, apn="free"):
        if not self.__connected:
            logger.error("Trying to call setupGPRS() while sim800 is not connected")
            return False
        if not self.__gprsReady:
            if not self.__attachGPRS():
                logger.error("Unable to attach GPRS")
                return False
            if not self.__activateBearerProfile(bearerId=1):
                logger.error("Unable to activate bearer profile")
                return False
            if not self.__setBearerAPN(apn=apn):
                logger.error("Unable to setup APN")
                return False
            if not self.__openBearer():
                logger.error("Unable to open Bearer")
                return False
            self.__gprsReady = True
        return True
    
    

    def httpGet(self, url=""):
        if not self.__connected:
            logger.error("Trying to call httpGet() while sim800 is not connected")
            return False
        if not self.__gprsReady:
            logger.error("Trying to use HTTP while GPRS is not configured")
            return False
        if not self.__httpInit():
            logger.error("HTTP: init failed")
            return False
        if not self.__httpBindBearer():
            logger.error("HTTP: Unable to bind bearer")
            return False
        if not self.__httpSetUrl(url=url):
            logger.error("HTTP: Unable to setup URL")
            return False
        status = self.__httpSendGetRequest()
        data = []
        if not status:
            logger.error("HTTP: Unable send GET request")
            return False
        elif status["httpStatusCode"] != 200:
            logger.warning("HTTP: GET request returned %d" % status["httpStatusCode"])
        else:
            response = self.__httpGetData()
            if response is False:
                logger.error("HTTP: Failed to read GET response")
                return False
            else:
                data = response
        ended = self.__httpEnd()
        return {"status":status["httpStatusCode"], "data": data, "httpEnded": ended}

    # FIXME
    def httpPost(self, url="", dataDict={}):
        if not self.__connected:
            logger.error("Trying to call httpPost() while sim800 is not connected")
            return False
        if not self.__gprsReady:
            logger.error("Trying to use HTTP while GPRS is not configured")
            return False


##################################################################
#                         Private methods                        #
##################################################################

    def __recovery(self):
        if not self.__ping():
            logger.error("Module ping failed. Reset configuration")
            self.__write("\x1b", end="")
            if not self.__resetDefaultConfig():
                logger.fatal("Unable to reset configuration.")
                self.__hardwareReset():
                time.sleep(5)
            if not self.__ping():
                logger.fatal("Module ping failed after reset. Abort")
                self.__serial.close()
                return False
        return True

    def __resetDefaultConfig(self):
        logger.warning("Reset module to default config")
        self.__write('ATZ')
        return self.__checkStatus()

    def __hardwareReset(self):
        GPIO.output(self.__resetPin, GPIO.LOW)
        time.sleep(0.25)
        GPIO.output(self.__resetPin, GPIO.HIGH)

    def __restartPowerSupply(self):
        if self.__psDisablePin is not None:
            GPIO.output(self.__psDisablePin, GPIO.HIGH)
            time.sleep(0.25)
            GPIO.output(self.__psDisablePin, GPIO.LOW)

    def __restartPowerSupply(self):

    
    def __ping(self, timeout=None):
        self.__write('AT')
        return self.__checkStatus()

##################################################################
#                            GSM methods                         #
##################################################################

    def __checkNewSms(self):
        s = self.__readline()
        if len(s) > 0:
            m = re.search(r"\+CMTI:.*,([0-9]+)", s)
            if m:
                index = int(m.groups()[0])
                logger.info("New message available at %d" % index)
                self.__availableSms.append(index)

    def __setupGSM(self):
        logger.info("Setup sim800 module")

        if not self.__disableURCPresentation():
            logger.error("Unable to disable URC presentation")
            return False

        if not self.__setTextMode():
            logger.error("Unable to set text mode")
            return False
        time.sleep(5)

        if not self.__setGSMMode():
            logger.error("Unable to set GSM mode")
            return False

        if not self.__setPhoneFunctionnalityState(True):
            logger.error("Unable to set phone functionality")
            return False

        if not self.__enableNewMessageIndication():
            logger.error("Unable to enable new message indication")
            return False

        for status in ["READ", "SENT", "UNSENT"]:
            if self.__fetchSms(status) != True:
                if not self.__deleteSms(status):
                    logger.error("Unable to delete %s SMS" % status)
                    return False

        unread = self.__fetchSms("UNREAD")
        if unread is False:
            logger.error("Unable to fetch unread SMS")
            return False
        else:
            self.__availableSms = unread
        logger.debug("Sim800 setup success")
        return True

    def __fetchSms(self, status):
        """fetch all unread sms without changing their state"""
        if status not in ["READ", "UNREAD", "SENT", "UNSENT", "ALL"]:
            logger.error("__fetchSms(): invalid argument: " + str(status))
            return False
        if "READ" in status:
            status = "REC " + status
        elif "SENT" in status:
            status = "STO " + status
        self.__write('AT+CMGL="%s",1' % status)
        s=1
        ret = []
        while s:
            s = self.__readline()
            if s == "OK":
                break
            elif s == "ERROR":
                return False
            indexes = re.findall(r'\+CMGL: ([0-9]+),', s, re.MULTILINE)
            for i in indexes:
                ret.append(int(i))
        
        return ret

    def __setBaudrate(self, baudrate=9600):
        self.__write("AT+IPR=%d" % baudrate)
        return self.__checkStatus()

    def __disableURCPresentation(self):
        self.__write('AT+CIURC=0')
        return self.__checkStatus(5)

    def __setTextMode(self):
        self.__write('AT+CMGF=1')
        return self.__checkStatus()

    def __setGSMMode(self):
        self.__write('AT+CSCS="GSM"')
        return self.__checkStatus(5)

    def __disableEcho(self):
        self.__write('ATE0')
        return self.__checkStatus()
        time.sleep(1)

    def __setPhoneFunctionnalityState(self, state=True):
        self.__write("AT+CFUN=%d" % (1 if state else 0))
        return self.__checkStatus()

    def __setSlowClockState(self, state=True):
        self.__write("AT+CSCLK=%d" % (1 if state else 0))
        return self.__checkStatus()

    def __deleteSms(self, status):
        if status not in ["READ", "UNREAD", "SENT", "UNSENT", "INBOX", "ALL"]:
            logger.error("__deletSms(): invalid argument: " + str(status))
            return False
        self.__write('AT+CMGDA="DEL %s"' % status)
        return self.__checkStatus()

    def __deleteSmsByIndex(self, index):
        self.__write('AT+CMGD=%d' % index)
        return self.__checkStatus()

    def __enableNewMessageIndication(self):
        self.__write('AT+CNMI=1')
        return self.__checkStatus()

##################################################################
#                           GPRS methods                         #
##################################################################

    def __attachGPRS(self, detach=False):
        self.__write('AT+CGATT=%d' % (0 if detach else 1))
        return self.__checkStatus()

    def __activateBearerProfile(self, bearerId=1):
        self.__gprsBearerId = bearerId
        self.__write('AT+SAPBR=3,%d,"CONTYPE","GPRS"' % self.__gprsBearerId)
        return self.__checkStatus()

    def __setBearerAPN(self, apn="free"):
        self.__write('AT+SAPBR=3,%d,"APN","%s"' % (self.__gprsBearerId, apn))
        return self.__checkStatus()

    def __checkBearer(self):
        self.__write('AT+SAPBR=2,%d' % self.__gprsBearerId)
        line = self.__readline() # echo
        line = self.__readline() # answer
        m = re.search(r'\+SAPBR: [0-9]+,([0-9]+),(.*)', line)
        status = None
        if m:
            g = m.groups()
            status = g[0]
            self.__ipAddress = g[1]
        atStatus = self.__checkStatus()
        if atStatus and status == "1":
            return True
        return False

    def __openBearer(self):
        if self.__checkBearer():
            return True
        self.__write('AT+SAPBR=1,%d' % self.__gprsBearerId)
        return self.__checkStatus()

    # To get local IP address

    def __setAPN(self, apn="free"):
        self.__write('AT+CSTT="%s","",""' % apn)
        return self.__checkStatus()

    def __enableWirelessConn(self):
        self.__write('AT+CIICR')
        return self.__checkStatus()

    def __getIPAddress(self):
        self.__write('AT+CIFSR')
        echo = self.__readline()
        response = self.__readline()
        if "ERROR"in response:
            return False
        else:
            return response

##################################################################
#                           HTTP methods                         #
##################################################################

    def __httpInit(self):
        self.__write('AT+HTTPINIT')
        if not self.__checkStatus():
            if self.__httpEnd():
                self.__write('AT+HTTPINIT')
                return self.__checkStatus()
        else:
            return True
        return False

    def __httpBindBearer(self):
        self.__write('AT+HTTPPARA="CID",%d' % self.__gprsBearerId)
        return self.__checkStatus()

    def __httpSetUrl(self, url=""):
        if url:
            self.__write('AT+HTTPPARA="URL","%s"' % url)
            return self.__checkStatus()
        return False

    def __setPostData(self, data):
        binaryData = bytearray(data, "utf-8")
        dataLen = len(binaryData)
        if datalen > 319488:
            logger.error("HTTP: Too many POST data to transmit at once")
            return False
        elif dataLen <= 0:
            logger.error("HTTP invalid POST data size")
            return False
        dataTransmitTime =  int(1.2 * float(dataLen + 2) * 8.0 / (self.__serialBaudrate * 8.0 / 10.0))
        if dataTransmitTime > 120:
            logger.error("HTTP: Transmit POSTS data to module will take too long. Reduce data size or increase baudrate")
            return False
        self.__write('AT+HTTPDATA=%d,%d' % (dataLen, dataTransmitTime))
        resp = self.__readline()
        if "DOWNLOAD" in resp:
            self.__serial.write(binaryData + "\r\n")

    def __httpSendGetRequest(self):
        return self.__httpSendRequest(0)

    def __httpSendPostRequest(self):
        return self.__httpSendRequest(1)

    def __httpSendHeadRequest(self):
        return self.__httpSendRequest(2)

    def __httpSendRequest(self, requestType=0):
        """ Param requestType:
            0: GET
            1: POST
            2: HEAD
        """
        self.__write('AT+HTTPACTION=%d' % requestType)
        if self.__checkStatus():
            response = self.__readline(10)
            m = re.search(r"HTTPACTION: ?([012]),([0-9]+),([0-9]*)", response)
            if m:
                g = m.groups()
                return {"requestType": int(g[0]),
                        "httpStatusCode": int(g[1]),
                        "dataLength": int(g[2])}
        return False

    def __httpGetData(self):
        self.__write('AT+HTTPREAD')
        echo = self.__readline()
        header = self.__readline()
        m = re.search(r"HTTPREAD: ([0-9]+)", header)
        data, dataLen = None, -1
        if m:
            dataLen = int(m.groups()[0])
            data = self.__readline()
        status = self.__checkStatus()
        return {"dataLen": dataLen,
                "data": data,
                "atStatus": status}

    def __httpEnd(self):
        self.__write('AT+HTTPTERM')
        return self.__checkStatus()

    
    
##################################################################
#                            UTILITIES                           #
##################################################################

    def __write(self, s, end="\r"):
        logger.debug(b"WRITE:" + bytearray(s+end, "ascii"))
        self.__serial.write(bytearray(s+end, "ascii"))

    def __readline(self, timeout=None):
        if timeout:
            timeout_bak = self.__serial.timeout # save current timeout
            self.__serial.timeout = timeout

        line = b'\r\n'
        while line == b'\r\n':
            line = self.__serial.read_until(b"\r\n")
            if line:
                l=line
                if l[-1] in b'\r\n':
                    l=l[:-1]
                logger.debug(b"READ :" + l)

        if timeout: # reset timeout to previous value
            self.__serial.timeout = timeout_bak

        if line[-2:] == b'\r\n':
            return line[:-2].decode(errors="ignore")
        return line.decode(errors="ignore")

    def __checkStatus(self, timeout=None):
        line = 1
        while line:
            line = self.__readline(timeout=timeout)
            if "OK" in line:
                return True
            elif "ERROR" in line:
                return False
        return False



##################################################################
#                          Debug methods                         #
##################################################################
    
    def dr(self):
        if not self.__serial.is_open:
            self.__serial.open()
        print(self.__getTextMode())
        print(self.__getGSMMode())
        print(self.__getPhoneFunctionnalityState())

    def dw(self):
        if not self.__serial.is_open:
            self.__serial.open()
        print(self.__setTextMode())
        print(self.__setGSMMode())
        print(self.__setPhoneFunctionnalityState(1))

    def dWrite(self, s):
        self.__write(s)

    def dRead(self, timeout=None):
        return self.__readline(timeout=timeout)

    def getSerial(self):
        return self.__serial

