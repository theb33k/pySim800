#!/usr/bin/python
# coding: utf-8

from __future__ import print_function
from builtins import input

import logging
import serial
import time
import re
import os
import RPi.GPIO as GPIO

logger = logging.getLogger("sim800")
GPIO.setwarnings(False)

class Sim800(object):
    GET=0
    POST=1
    HEAD=2
    def __init__(self, device, resetPin=27, powerSupplyResetPin=22):
        self.__device = device
        self.__serial = None
        self.__serialBaudrate = 9600
        self.__availableSms = []
        self.__serialReady = False
        self.__gsmReady = False
        self.__gprsReady = False
        self.__gprsBearerId = None
        self.__ipAddress = "0.0.0.0"
        # setup reset pin
        self.__resetPin = resetPin
        GPIO.setmode(GPIO.BCM)  
        GPIO.setup(self.__resetPin, GPIO.OUT, pull_up_down=GPIO.PUD_OFF)
        GPIO.output(self.__resetPin, GPIO.HIGH) # reset is active LOW
        # setup powerSupply pin
        self.__powerSupplyResetPin = powerSupplyResetPin
        if self.__powerSupplyResetPin is not None:
            GPIO.setup(self.__powerSupplyResetPin, GPIO.OUT, pull_up_down=GPIO.PUD_OFF)
            GPIO.output(self.__powerSupplyResetPin, GPIO.LOW) # PS is OFF when pin is HIGH
    
    def begin(self, device=None, baudrate=None, timeout=2):
        """ Open serial and start init procedure for Sim800L module:
        This method MUST be called before any other
        1. reboot device (restart power supply)
        2. reset All params to default configuration
        3. Check Ping
            if KO: Enter recovery mode, ie. restart step 1. and 2.
        4. Configure GSM
        """
        if not self.__serialReady:
            if device:
                self.__device = device
            if baudrate:
                self.__serialBaudrate = baudrate
            self.__serial = serial.Serial(self.__device, baudrate=self.__serialBaudrate, timeout=timeout)
            self.__serial.reset_input_buffer()
            self.__serialReady = True
        
        self.__resetPowerSupply() # Reset will re-enable unsollicited codes
        time.sleep(5)
        
        if not self.__gsmReady:
            if not self.__ping():
                if not self.__recovery():
                    self.__serial.close()
                    return False
            else:
                self.__resetDefaultConfig() # reset all params to default
            if self.__setupGSM():
                self.__gsmReady = True
                return True
        else:
            if timeout and timeout != self.__serial.timeout:
                self.__serial.timeout = timeout
            return True
        self.__serial.close()
        return False

    def stop(self):
        self.__httpEnd()
        if self.__gprsReady:
            self.__attachGPRS(detach=True)
            self.__gprsReady = False
        if self.__gsmReady:
            self.__setPhoneFunctionnalityState(False)
            self.__gsmReady = False
        self.__ipAddress = "0.0.0.0"
        if self.__serialReady:
            self.__serial.close()
            self.__serialReady = False
        return True

    def available(self):
        if self.__gsmReady:
            self.__checkNewSms()
            return len(self.__availableSms)
        else:
            logger.error("Trying to call available() while sim800 is not connected")
            return False

    def readSms(self): 
        """reads the oldest unread sms and delete it from sim800 module"""
        if self.__gsmReady:
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
        if self.__gsmReady:
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
        if self.__gsmReady:
            return self.__deleteSms("ALL")
        else:
            logger.error("Trying to call flush() while sim800 is not connected")
            return False

    def isOpen(self):
        return self.__gsmReady == True    

    def httpGet(self, url):
        """ Send GET request to url
        returns (status, data):
            status: HTTP error code
            data: Either None or a bytearray
        """
        if not self.__gsmReady:
            logger.error("Trying to call httpGet() while sim800 is not connected")
            return (0,0)
        if not self.__gprsReady:
            res = self.__setupGPRS()
            if not res:
                logger.error("Trying to use HTTP while GPRS is not configured")
                return (0,0)
        if not self.__httpInit():
            logger.error("HTTP: init failed")
            return (0,0)
        if not self.__httpBindBearer():
            logger.error("HTTP: Unable to bind bearer")
            return (0,0)
        if not self.__httpSetUrl(url=url):
            logger.error("HTTP: Unable to setup URL")
            return (0,0)
        status, dataLength = self.__httpSendRequest(self.GET)
        data = []
        if not status:
            logger.error("HTTP: Unable send GET request")
            return (0,0)
        elif status != 200:
            logger.warning("HTTP: GET request returned %d" % status)
        else:
            ok, data = self.__httpReadData()
            if not ok:
                logger.error("HTTP: Failed to read GET response")
                return (0,0)
        self.__httpEnd()
        return (status, data)

    def httpPost(self, url, data, contentType="text/plain"):
        """ Send POST request to url
        returns (status, data):
            status: HTTP error code
            data: Either None or a bytearray
        """
        if not self.__gsmReady:
            logger.error("Trying to call httpPost() while sim800 is not connected")
            return (0,0)
        if not self.__gprsReady:
            res = self.__setupGPRS()
            if not res:
                logger.error("Trying to use HTTP while GPRS is not configured")
                return (0,0)
        if not self.__httpInit():
            logger.error("HTTP: init failed")
            return (0,0)
        if not self.__httpBindBearer():
            logger.error("HTTP: Unable to bind bearer")
            return (0,0)
        if not self.__httpSetUrl(url=url):
            logger.error("HTTP: Unable to setup URL")
            return (0,0)
        if not self.__httpSetPostData(data, contentType=contentType):
            logger.error("HTTP: Unable to write post data")
            return (0,0)

        status, dataLength = self.__httpSendRequest(self.POST)
        ok, data = self.__httpReadData() # debug
        data = []
        if not status:
            logger.error("HTTP: Unable send POST request")
            return (0,0)
        elif status != 200:
            logger.warning("HTTP: POST request returned %d" % status)
        else:
            ok, data = self.__httpReadData()
            if not ok:
                logger.error("HTTP: Failed to read POST response")
                return (0,0)
        self.__httpEnd()
        return (status, data)

##################################################################
#                         Private methods                        #
##################################################################

    def __recovery(self):
        logger.info("Recovering from error")
        if not self.__ping():
            logger.warning("Module ping failed.")
            self.__write("\x1b", end="")
            if not self.__resetDefaultConfig():
                self.__hardwareReset()
                time.sleep(5)
            if not self.__ping():
                logger.warning("Module ping failed.")
                self.__resetPowerSupply()
                time.sleep(10)
                if not self.__ping():
                    logger.warning("Module ping failed.")
                    logger.fatal("Still no ping after restart. Abort.")
                    self.__serial.close()
                    return False
        logger.info("Successfully recovered")
        return True

    def __resetDefaultConfig(self):
        logger.warning("Reset module to default config")
        self.__write('ATZ')
        return self.__checkStatus()

    def __hardwareReset(self):
        logger.warning("Module hardware reset")
        GPIO.output(self.__resetPin, GPIO.LOW)
        time.sleep(0.25)
        GPIO.output(self.__resetPin, GPIO.HIGH)

    def __resetPowerSupply(self):
        logger.warning("Restart Module power supply")
        if self.__powerSupplyResetPin is not None:
            GPIO.output(self.__powerSupplyResetPin, GPIO.HIGH)
            time.sleep(0.25)
            GPIO.output(self.__powerSupplyResetPin, GPIO.LOW)
            return True
        else:
            return False

    def __ping(self, timeout=None):
        self.__write('AT')
        ret = self.__checkStatus()
        if ret:
            self.__serialReady = True
        else:
            self.__serialReady = False
        return ret

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
        logger.info("Setup GSM")

        if not self.__disableURCPresentation(): # Disable Call Ready message
            logger.error("Unable to disable URC presentation")
            return False

        if not self.__setPhoneFunctionnalityState(True):
            logger.error("Unable to set phone functionality")
            return False
        self.__waitFor("SMS Ready", timeout=10)

        if not self.__setTextMode():
            logger.error("Unable to set text mode")
            return False
        time.sleep(5)

        if not self.__setGSMMode():
            logger.error("Unable to set GSM mode")
            return False

        if not self.__enableNewMessageIndication(): # Enable SMS Ready message
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
            logger.info("%d Unread SMS" % len(unread))
        logger.debug("Sim800 setup success")
        return True

    def __fetchSms(self, status):
        """Fetch all unread sms without changing their state
        Param status:
            READ
            UNREAD
            SENT"
            UNSENT
            ALL
        """
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
    
    def __getTextMode(self):
        self.__write("AT+CMGF?")
        r = self.__waitFor(r"CMGF: [01]", regex=True)
        textMode = None
        if r:
            if r[0] == "0":
                textMode = "pdu"
            else:
                textMode = "text"
        else:
            logger.error("Unable to read text mode")
        status = self.__rcheckStatus()
        return textMode
        
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
        """Delete SMS with status:
        Param:
            READ
            UNREAD
            SENT
            UNSENT
            INBOX
            ALL
        """
    
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

    def __setupGPRS(self, apn="free"):
        if not self.__gsmReady:
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
            cmdStatus, bearerId, bearerStatus, ipAddress = self.__getBearerSatus()
            if cmdStatus and bearerStatus != 1: # Not connected yet
                if not self.__openBearer():
                    logger.error("Unable to open Bearer")
                    return False
                time.sleep(1)
                # wait for connection (ie. bearerStatus = 1)
                start = time.time()
                while bearerStatus != 1 and time.time() - start < 10:
                    cmdStatus, bearerId, bearerStatus, ipAddress = self.__getBearerSatus()
                    time.sleep(2)
            self.__ipAddress = ipAddress
            self.__gprsReady = True
        return True

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

    def __getBearerSatus(self):
        """ Get Brearer current status
        Returns a tuple with:
        0: The AT command status: True or False (following fields are invalid)
        1: the bearer id
        2: The bearer status:
            0: connecting
            1: connected
            2: closing:
            3: closed
        3: the ip address
        """
        self.__write('AT+SAPBR=2,%d' % self.__gprsBearerId)
        r = self.__waitFor(r'\+SAPBR: ([0-9]+),([0-9]+),(.*)', regex=True)
        bearerStatus = None
        if r:
            bearerId = int(r[0])
            bearerStatus = int(r[1])
            ipAddress = r[2]
        else:
            bearerId = None
            bearerStatus = None
            ipAddress = "0.0.0.0"
        # logger.debug("Enable bearer: status: %s, IP:%s" % (bearerStatus, self.__ipAddress))
        # logger.debug("Test readIpAddress")
        # self.__readIPAddress()
        # time.sleep(5)
        # self.__readIPAddress()

        return (self.__checkStatus(), bearerId, bearerStatus, ipAddress)

    def __openBearer(self):
        self.__write('AT+SAPBR=1,%d' % self.__gprsBearerId)
        return self.__checkStatus(timeout=5)

    # To get local IP address

    def __setAPN(self, apn="free"):
        self.__write('AT+CSTT="%s","",""' % apn)
        return self.__checkStatus()

    def __enableWirelessConn(self):
        self.__write('AT+CIICR')
        return self.__checkStatus()

    # FIXME: Use __waitFor method
    def getIPAddress(self):
        return self.__ipAddress
        # self.__write('AT+CIFSR')
        # echo = self.__readline()
        # response = self.__readline()
        # if "ERROR"in response:
        #     return False
        # else:
        #     self.__ipAddress = response
        #     return True

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

    def __httpSetUrl(self, url):
        self.__write('AT+HTTPPARA="URL","%s"' % url)
        return self.__checkStatus()
    
    def __httpSetContentType(self, contentType):
        """Set HTTP MIME type:
        Param mime:
            * application/json
            * application/octet-stream
            * text/plain
            * ... cf HTTP standard
        """
        self.__write('AT+HTTPPARA="CONTENT","%s"' % contentType)
        return self.__checkStatus()

    def __httpSetPostData(self, data, contentType=None):
        if isinstance(data, str) or isinstance(data, unicode):
            serialData = bytearray(data, "utf-8")
            if not contentType:
                contentType = "text/plain"
        else:
            serialData = bytearray(data)
            if not contentType:
                contentType = "application/octet-stream"

        if not self.__httpSetContentType(contentType):
            logger.error("Unable to set HTTP Content-type")
            return False

        dataLen = len(serialData)
        if dataLen ==0 or dataLen > 319488:
            logger.error("HTTP: POST data must be between 1 and 319488 bytes")
            return False

        bitsToTransmit = float(dataLen + 2) * 8.0
        averageBitRate = float(self.__serialBaudrate) * 8.0 / 10.0 #  8data bits + 1 start + 1 stop
        dataTransmitTime = 1000 + 1000.0 * bitsToTransmit / averageBitRate
        if dataTransmitTime > 120000:
            logger.error("HTTP: Transmit POSTS data to module will take too long. Reduce data size or increase baudrate")
            return False
        # dataTransmitTime = 10000
        self.__write('AT+HTTPDATA=%d,%d' % (dataLen, dataTransmitTime))
        if self.__waitFor("DOWNLOAD"):
            self.__serial.write(serialData)
            time.sleep(dataTransmitTime/1000.0)
            return True
        return False

    def __httpSendRequest(self, requestType=0):
        """ Param requestType:
            0: GET
            1: POST
            2: HEAD
        """
        self.__write('AT+HTTPACTION=%d' % requestType)
        if self.__checkStatus():
            r = self.__waitFor(r"\+HTTPACTION: ?[012],([0-9]+),([0-9]*)", timeout=20, regex=True)
            if r:
                return (int(r[0]), int(r[1])) # (status, data length)
        return (0, 0)

    def __httpReadData(self):
        self.__write('AT+HTTPREAD')
        r = self.__waitFor(r"HTTPREAD: ([0-9]+)", regex=True)
        if r:
            length = int(r[0])
            data = bytearray(self.__serial.read(length))

        return (self.__checkStatus(), data)

    def __httpEnd(self):
        self.__write('AT+HTTPTERM')
        return self.__checkStatus()

    

    
    
##################################################################
#                            UTILITIES                           #
##################################################################

    def __write(self, s, end="\r"):
        logger.debug(b"WRITE:" + bytearray(s+end, "utf-8"))
        self.__serial.write(bytearray(s+end, "utf-8"))

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

    def __waitFor(self, responses, timeout=None, regex=False):
        """Wait for a specific module answer
        Params:
            * response: a string or a list of strings
            * timeout: maximum time to wait for the response
        Returns: the matching response, or None
        Info:
            * This methos will throw away everithing send by the module until match or timeout
            * Remove all \r\n
        """
        if not isinstance(responses, list):
            responses = [responses]
        if not timeout:
            timeout = self.__serial.timeout
        start = time.time()
        ret = None
        while not ret and time.time()-start < timeout:
            resp = self.__readline()
            for expected in responses:
                if regex:
                    m = re.search(expected, resp)
                    if m:
                        return m.groups()
                else:
                    if resp == expected:
                        return resp
        return None

    def __checkStatus(self, timeout=None):
        if self.__waitFor(["OK", "ERROR"], timeout=timeout) == "OK":
            return True
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

