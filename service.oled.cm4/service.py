#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from PIL import ImageFont, Image, ImageDraw, ImageOps
from time import sleep, time as ttime
from threading import Thread
from re import search, compile
from  selectors import DefaultSelector, EVENT_READ
import socket
from xbmc import LOGDEBUG, LOGINFO, log as log_, Monitor
from xbmcvfs import translatePath
from xbmcaddon import Addon
from os.path import join, isfile
from queue import Queue, Empty


#This must be correlated with the settings.xml file:
DRIVERS = ('sh1106', 'ssd1306', 'ssd1309', 'ssd1327', 'sh1107', 'ssd1362', 'ssd1322', 'ssd1322_nhd', 'ssd1325', 'ssd1351', 'sh1122')
RESOLS = ((128, 128), (128, 64), (128, 32), (64, 128), (256, 64), (256, 48), (256, 32), (128, 96), (128, 48))

def addon():
    return Addon(id = 'service.oled.cm4')

DEBUG = addon().getSetting('debug') in (True, 'true')
TIME_RE = compile(r'^(([0-1]?[0-9])|([2][0-3])):([0-5]?[0-9])(:([0-5]?[0-9]))?$')


def log(message, debug = True):
    if debug:
        if DEBUG:
            level = LOGDEBUG
        else:
            return
    else:
        level = LOGINFO
    log_("####### [%s] - %s" % (addonname, message), level = level)

def ceil(x, w):
    return int(x / w) + int(x % w > 0)

def LANG(id):
    return addon().getLocalizedString(id)

def istime(s):
    if len(s) > 8:
        return False
    return bool(TIME_RE.match(s))

addonname   = addon().getAddonInfo('name')
images_dir = join(addon().getAddonInfo('path'), 'resources','images')
fonts_dir = join(addon().getAddonInfo('path'), 'resources','fonts')


class Oled(object):
    images = None
    frames = None
    states = None
    rowdata = None
    scroll = False
    device = None
    bigdigits = 0
    bigfont = None
    bigpos = (0, 0)
    icons = None
    brackets = False
    wid = 16
    cellwid = 8
    barr = None
    q = None
    bcklghts = [25, 125]
    bcklght = 1
    last = 0
    delay = 0.1

    def __init__(self, iface, dev, i2cport, i2caddress, spiport, spidevice, dc, rst, width, height, rotate, rows, regpath, monopath, bcklght1, bcklght0):
        if iface: #SPI
            from luma.core.interface.serial import spi
            serial = spi(gpio_DC=dc, gpio_RST=rst,device=spidevice, port=spiport)
        else: # I2C
            from luma.core.interface.serial import i2c
            serial = i2c(port = i2cport, address = i2caddress)
        import importlib
        devmodule = importlib.import_module('luma.oled.device')
        driver  = getattr(devmodule, DRIVERS[dev])
        self.device = driver(serial, width, height, rotate, mode = '1')
        self.w = max(width, height)
        self.step = self.w/16
        self.h = min(width, height)
        self.rows = rows
        self.bcklght = 1
        self.setbcklght(bcklght1, bcklght0)
        self.lh = int(self.h/self.rows)
        self.bigdigits = 0
        self.q = Queue()
        self.reset()
        self.finished = False
        self.ct = None
        self.iconfont = ImageFont.truetype(join(fonts_dir, "Playericons.ttf"), self.lh - 2)
        fontpath = regpath if isfile(regpath) else join(fonts_dir, "Atozimple Regular.otf")
        self.monopath = monopath if isfile(monopath) else join(fonts_dir, "LiberationMono-Regular.ttf")
        self.font = ImageFont.truetype(fontpath, self.lh - 2, encoding = "unic")
        self.timefont = ImageFont.truetype(self.monopath, self.lh - 2)
        self.timepos = int(self.iconfont.getlength(" 3   "))
        self.timew = int(self.timefont.getlength("00:00:00"))
        self.wid, self.cellwid = int(self.w/8), 8
        self.image = Image.new('1', ((self.w, self.h)), 'black')
        self.mysel = DefaultSelector()
        self.keep_running = True
        server_address = ('127.0.0.1', 13666)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.setblocking(False)
        server.bind(server_address)
        server.listen(5)
        self.mysel.register(server, EVENT_READ, self.accept)
        Thread(target = self.runserver).start()
        Thread(target = self.runqueue).start()
        logopath = join(images_dir, 'Logo%i.png' % self.h)
        if dev in (0, 1, 2, 4):
            logo = Image.open(logopath).convert("1")
        else:
            logo = Image.open(logopath)
            logo = ImageOps.grayscale(logo)
        posn = ((self.device.width - logo.width) // 2, 0)
        self.image.paste(logo, posn)
        self.device.display(self.image)

    def setbcklght(self, on, off):
        self.bcklghts[1] = on
        self.bcklghts[0] = off
        self.device.contrast(self.bcklghts[self.bcklght])

    def contrast(self, val):
        self.bcklght = val
        self.device.contrast(self.bcklghts[val])

    def runqueue(self):
        while self.keep_running:
            if self.scroll:
                self.last = ttime()  #scrolling begin
                for row in range(self.rows):
                    if self.frames[row] > 1:
                        self.rowdata[row]['state'] += 1
                        if self.rowdata[row]['state'] >= int(self.w * self.frames[row] / self.step):
                            self.rowdata[row]['state'] = 0
                        self.image.paste(self.rowdata[row]['img'].crop((self.step*self.rowdata[row]['state'], 0, self.w+self.step*self.rowdata[row]['state'], self.lh)), (0, row * self.lh))
                self.device.display(self.image)  #scrolling end
                dif = ttime() - self.last
                tm = self.delay - dif if dif < self.delay else 0
                try:
                    task = self.q.get(True, tm)
                except Empty:
                    continue
                else:
                    if task == "STOP":
                        break
                    self.processdata(task)
                    dif = ttime() - self.last
                    if dif < self.delay:
                        sleep(self.delay - dif)
            else:
                task = self.q.get()
                if task == "STOP":
                    break
                self.processdata(task)
        self.finished = True

    def progress_bar(self, line, txt):
        kind, offset, num = txt.split(' ')
        img = self.progress_line()[0] if kind == '[*PRGRSS#]' else self.images[line]
        img = img if img is not None else Image.new('1', ((self.w, self.lh)), 'black')
        num = int(num)
        if num < self.barr[line]: # clear progressbar
            if self.brackets:
                x = self.cellwid
                ln = self.w - 2 * self.cellwid
            else:
                x = 0
                ln = self.w
            im = Image.new('1', (ln, self.lh), 'black')
            img.paste(im, (x, 0))
        im = Image.new('1', ((num, int(self.lh / 2))), 'white')
        img.paste(im, (('1', '2').index(offset)*self.cellwid, int(self.lh / 4)))
        self.barr[line] = num
        return img, 1, -1

    def progress_line(self):
        im = Image.new('1', ((self.w, self.lh)), 'black')
        ImageDraw.Draw(im).text((0, 0), '[', fill='white', font = self.timefont)
        iml = im.crop((0, 0, self.cellwid, self.lh))
        imr = ImageOps.mirror(iml)
        im.paste(imr, (self.w-self.cellwid - 1, 0))
        return im, 1, -1

    def processdata(self, args):
        log("processdata: args = {}".format(repr(args)))
        flag = False
        if args[0].count(True) > 0:
            flag = True
            for i in range(self.rows):
                if args[0][i]:
                    self.clearline(i)
        if args[2].count(-1) < self.rows :
            flag = True
            for i in range(self.rows):
                icn = args[2][i]
                self.icons[i] = icn
                if  icn > -1:
                    txt = " %i  " % icn
                    ImageDraw.Draw(self.images[i]).text((0, 0), txt, fill='white', font = self.iconfont)
                    self.image.paste(self.images[i], (0, i * self.lh))
        if args[1].count(None) < self.rows :
            flag = True
            for i in range(self.rows):
                text = args[1][i]
                if text is not None:
                    if text == '[*PROGRESS*]':
                        self.images[i], self.frames[i], self.states[i] = self.progress_line()
                    elif text.startswith('[*PRGRSS'):
                        self.images[i], self.frames[i], self.states[i] = self.progress_bar(i, text)
                    else:
                        self.barr[i] = 256
                        self.images[i], self.frames[i], self.states[i] = self.text_line(text) if self.icons[i] == -1 else self.player_line(i, text)
                        if self.frames[i] > 1:
                            self.rowdata[i] = {'img':self.images[i],  'state':self.states[i]}
                            self.images[i] = None
        if flag:
            for i in range(self.rows):
                if self.images[i] is not None:
                    self.image.paste(self.images[i], (0, i * self.lh))
            if [it > 1 for it in self.frames].count(True) == 0:
                self.scroll = False
            else:
                self.scroll = True
        bignums = args[3]
        if bignums.count(None) < self.bigdigits:
            size = self.bigfont.getbbox('0')[2:]
            for i in range(self.bigdigits):
                im = Image.new('1', size, 'black')
                x = bignums[i]
                if x is not None:
                    ImageDraw.Draw(im).text((0, 0), x, fill='white', font = self.bigfont)
                    self.image.paste(im, (i * size[0] + self.bigpos[0], self.bigpos[1]))
        self.device.display(self.image)

    def parsecommand(self, cmd):
        log("parsecommand: cmd = {}".format(repr(cmd)))
        num = None
        clrlines = self.rows*[False]
        lines = self.rows*[None]
        bignums = self.bigdigits * [None] if self.bigdigits > 0 else []
        icons = self.rows * [-1]
        for itm in cmd.split(b'\n'):
            if itm.startswith(b'widget_set xbmc'):
                parts=itm[16:].split(b' ')
                x = search(b'(lineBigDigit)([1-8])|(lineProgress|lineScroller|lineIcon)([1-4])', parts[0])
                if x is not None:
                    if x.groups()[3] is not None:
                        i = int(x.groups()[3]) - 1
                        if x.groups()[2]== b'lineProgress':
                            if parts[1:] == [b'0', b'0', b'0']:
                                clrlines[i] = True
                            else:
                                flag = '#' if lines[i] == '[*PROGRESS*]' else '*'
                                lines[i] = '[*PRGRSS{}] {} {}'.format(flag, parts[1].decode(), parts[3].decode())
                        elif x.groups()[2]== b'lineIcon':
                            icons[i] = [b'BLOCK_FILLED', b'STOP', b'PLAY', b'PAUSE', b'FF', b'FR'].index(parts[-1]) - 1
                            if parts[1:] == [b'0', b'0', b'BLOCK_FILLED']:
                                clrlines[i] = True
                        elif x.groups()[2]== b'lineScroller':
                            tmp = itm[16:].replace(b'\ ', b'\x1a').replace(b'[              ]', b'[*PROGRESS*]')
                            parts = tmp.split(b' ')
                            parts[-1] = parts[-1].replace(b'\x1a', b' ').replace(b'\\', b'')
                            if parts[-1][0]==parts[-1][-1]==34:
                                parts[-1]=parts[-1][1:-1]
                            if parts[-1] == b'':
                                clrlines[i] = True
                            else:
                                self.delay = 0.1 * int(parts[-2])
                                if parts[-1] == b'[*PROGRESS*]': # progressbarr with brackets
                                    clrlines[i] = True
                                    self.brackets = True
                                try:
                                    lines[i] = parts[-1].decode('utf-8')
                                except:
                                    log("UTF-8 decode error: {}".format(repr(parts[-1])))
                                    lines[i] = ""
                    elif x.groups()[0]==b'lineBigDigit':
                        i = int(x.groups()[1]) - 1
                        bignums[i] = ' ' if parts[1:] == [b'0', b'0'] else parts[-1].decode() if int(parts[-1]) < 10 else ':'
            elif itm.startswith(b'widget_add xbmc lineBigDigit'):
                parts = itm[28:].split(b' ')
                num = int(parts[0])
                if num > self.bigdigits:
                    self.bigdigits = num
                    bignums.append(None)
        if num is not None:
            self.bigfont, self.bigpos  = self.big_font()
        self.q.put((clrlines, lines, icons, bignums))

    def read(self, connection, mask):
        # Callback for read events
        data = connection.recv(2048)
        if data:
            # A readable client socket has data
            res = None
            if data == b"hello\n":
                res = "connect LCDproc 0.5dev protocol 0.4 lcd wid {} hgt {} cellwid {} cellhgt {}\n".format(self.wid, self.rows, self.cellwid, int(self.h/self.rows)).encode()
            elif data == b"info\n":
                res = b"OLEDproc\n"
            elif data == b'noop\n':
                res = b'noop complete\n'
            elif data == b'screen_add xbmc\n':
                pass
            elif data == b'screen_set xbmc -priority info\n':
                pass
            elif data.startswith(b'screen_set xbmc -heartbeat'):
                pass
            elif data.startswith(b'screen_set xbmc -backlight on\n'):
                self.contrast(1)
            elif data.startswith(b'screen_set xbmc -backlight off\n'):
                self.contrast(0)
            elif data == b"bye\n":
                self.mysel.unregister(connection)
                connection.close()
                self.clear()
                return
            else:
                self.parsecommand(data)
            if res is not None:
                connection.sendall(res)
            else:
                for i in range(data.count(b"\n")):
                    connection.sendall(b'success\n')
        else:
            # Interpret empty result as closed connection
            self.mysel.unregister(connection)
            connection.close()
            self.clear()

    def accept(self, sock, mask):
        # callback for new connections
        new_connection, addr = sock.accept()
        new_connection.setblocking(False)
        self.mysel.register(new_connection, EVENT_READ, self.read)

    def runserver(self):
        while self.keep_running:
            for key, mask in self.mysel.select(timeout = 1):
                callback = key.data
                callback(key.fileobj, mask)
        self.mysel.close()

    def reset(self):
        with self.q.mutex:
            self.q.queue.clear()
        self.images = self.rows * [None]
        for i in range(self.rows):
            self.images[i] = Image.new('1', ((self.w, self.lh)), 'black') 
        self.frames = self.rows * [1]
        self.states = self.rows*[None]
        self.rowdata = self.rows*[{}]
        self.scroll = False
        self.icons = self.rows * [-1]
        self.barr = self.rows * [256]

    def clear(self):
        self.device.clear()
        self.reset()

    def clearline(self, row):
        self.images[row] = Image.new('1', ((self.w, self.lh)), 'black')
        self.icons[row] = -1
        self.frames[row] = 1
        self.states[row] = None
        self.rowdata[row] = {}
        self.barr[row] = 256

    def player_line(self, line, timestr):
        im = Image.new('1', ((self.w, self.lh)), 'black')
        txt = " %i  " % self.icons[line]
        ImageDraw.Draw(im).text((0, 0), txt, fill='white', font = self.iconfont)
        ImageDraw.Draw(im).text((self.timepos, 0), timestr, fill='white', font = self.timefont)
        return im, 1, -1

    def text_line(self, text):
        if istime(text):
            im = Image.new('1', ((self.w, self.lh)), 'black')
            ImageDraw.Draw(im).text((0, 0), text, fill='white', font = self.timefont)
            return im, 1, -1
        fr = ceil(self.font.getlength(text), self.w)
        im = Image.new('1', ((self.w if fr==1 else self.w*(1+fr), self.lh)), 'black')
        ImageDraw.Draw(im).text((0, 0), text, fill='white', font = self.font)
        if fr > 1:
            im.paste(im.crop((0, 0, self.w, self.lh)), (self.w*fr, 0))
        return im, fr, -1

    def big_font(self):
        lngth = self.w + 1
        fh = self.h + 1
        while lngth > self.w:
            fh -= 1
            fnt = ImageFont.truetype(self.monopath, fh)
            lngth = int(fnt.getlength(self.bigdigits * '0'))
        h = fnt.getbbox('0')[3]
        pos = (int((self.w - lngth)/2), int((self.h - h)/2))
        return fnt, pos

    def stop(self):
        self.q.put("STOP")
        self.keep_running = False
        if self.ct is not None and self.ct.is_alive():
            self.ct.cancel()
        if True in [it > 1 for it in self.frames]:
            self.scroll = False
            while not self.finished:
                sleep(0.1)
        self.clear()
        self = None
        del(self)


class BackgroundService(Monitor):
    config = None
    def __init__(self):
        Monitor.__init__(self)
        log(LANG(30083), False)
        args = self.GetSettings()
        self.config = args
        self.oled = Oled(*args)

    def GetSettings(self):
        ok = addon().getSettingBool('ok')
        if not ok:
            addon().setSettingBool('ok', True)
            addon().openSettings()
        interface = int(addon().getSetting('interface'))
        chip = int(addon().getSetting('driverchip{}'.format(interface)))
        port = int(addon().getSetting('i2cport'))
        addr = int(addon().getSetting('address'))
        spiport = int(addon().getSetting('spiport'))
        spidevice = int(addon().getSetting('spidevice'))
        dc = int(addon().getSetting('dc40')) 
        rst = int(addon().getSetting('res40')) 
        resol = int(addon().getSetting('chip{}'.format(chip)))
        w, h = RESOLS[resol]
        rotate =int (addon().getSetting(('rotation4','rotation2','rotation2','rotation3','rotation2','rotation2','rotation2','rotation2','rotation2')[resol]))
        rows = int(addon().getSetting('lines{}'.format(min(w, h))))
        bcklght1 = int(addon().getSetting('bcklght1'))
        bcklght0 = int(addon().getSetting('bcklght0'))
        regpath = addon().getSetting('regfont')
        monopath = addon().getSetting('monofont')
        return interface, chip, port, addr, spiport, spidevice, dc, rst, w, h, rotate, rows, regpath, monopath, bcklght1, bcklght0

    def onSettingsChanged(self):
        log(LANG(30085), False)
        global DEBUG
        DEBUG = addon().getSetting('debug') in (True, 'true')
        args = self.GetSettings()
        if args[:-2] == self.config[:-2]:
            self.oled.setbcklght(*args[-2:])
        else:
            self.oled.stop()
            sleep(1)
            del(self.oled)
            self.oled = Oled(*args)
        self.config = args

    def stop(self):
        self.oled.stop()
        sleep(1)
        del(self.oled)
        del(self)

if __name__ == '__main__':
    monitor = BackgroundService()
    msg = LANG(30084)
    while not monitor.abortRequested():
        monitor.waitForAbort(10)
    log(msg, False)
    monitor.stop()
    sys.exit()
