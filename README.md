# Deepgram Subtitle Hoodie
*Real-time subtitles on your chest!*

The worst part about sound-based communication is that it doesn't always work. Sometimes a potential conversational partner is hard of hearing, sometimes the room is loud, sometimes they're sticking their fingers in their ears and are repeating "I can't hear you!" ***What is an attention-starved nerd to do?***

Stick a display on your chest and use an automated speech recognition system to transcribe everything you say into real-world subtitles, obviously.

I built this stupid project for a [YouTube video sponsored by Deepgram](https://www.youtube.com/watch?v=mTK8dIBJIqg), so I don't really recommend building one yourself. But if you absolutely must, you'll need:

* A Deepgram API key: https://dpgr.am/Voidstarlab
* A Codec Zero sound card: https://bit.ly/3sAOXkY
* Some Chicago nuts: https://amzn.to/3ljAMww
* This old-skool OSD font (I couldn't include it in the repo): https://bit.ly/3MoyCrp
* A power bank, such as: https://bit.ly/38voK0p
* A stretched-bar display: https://bit.ly/3yDnnra
* A Raspberry Pi 3B+ with SD card loaded with latest Raspberry Pi OS 
* Stranahan's Colorado Single Malt: https://bit.ly/3Nflfde (optional)
* HDMI and power cables
* An electret lavalier mic: https://amzn.to/3a1i4aB
* A tightish-fitting pullover hoodie or T-shirt made of thicc fabric 
* 4x 15mm-long countersunk M2.5 screws

## Building the hardware:

* If you procured the whiskey, drink some of it. It's good stuff.
* Print one copy of each of the six STLs in the Models folder. You can use any rigid filament. Supports aren't needed. To save weight, use 5% infill and two perimeters.
* Use the Templates to figure out where to position the display and Pi. I recommend putting the Pi on your right shoulder and the display centered on your chest, about 10mm below the neckline.
* Mark the Template holes and perforate your garment. 
* You don't need to cut out the big rectangular hole for the Display, but you can do it anyways if you're having the kind of rough day that makes you want to cut a big hole in your hoodie.
* Use four of the screws and standoffs included with the Codec Zero to mount your Pi to the Pi Case Lower.
* Add the Codec Zero and plug in the mic.
* Plop the Pi Case Upper on top and use the long screws to fasten it. There's a little hole near the Pi's headphone jack for the mic. Feel free to tie a strain-relief knot in the mic cable.
* Use Chicago screws to mount the Pi to the hoodie. If it's threatening to pull off, you can add the template beneath the fabric to keep it in place.
* Peel the adhesive tape off the display and stick it to the Display Case Frame.
* Put the Display Case Bacc in the hoodie and line it up with the holes. Shove a Chicago nut through the Bacc and through a hole.
* Plop the Frame onto the hoodie, threading the Chicago nut through the corresponding hole. Fasten.
* Add the rest of the screws.
* Connect the display to the Pi with HDMI and USB. Use the shortest possible cables so they don't snag.
* Connect the Pi to your power bank with a soft USB cable
* Follow instructions below to put the software on the Pi
* Go out in public and impress total dweebs

## Preparing the Pi:

* Connect the Pi to Wi-Fi, unless you like wiring yourself to the wall via Ethernet
* Update Pygame: `pip install pygame --upgrade`
* ~~~Own~~~ Install the libs: `pip install pyaudio deepgram` 
* Make folders: `mkdir /home/pi/hoodie/res`
* Download everything in the repo except the models and move 'em to /home/pi/hoodie
* `cd /home/pi/hoodie; chmod +x main.py install-service.py`
* Download the font VCR_OSD_MONO-1.001.ttf and move it to /home/pi/hoodie/res. Don't you dare rename it. It's beautiful just the way it is.
* `sudo ./install-service.py`
* Edit main.py to replace PUT YOUR API KEY HERE YOU JABRONI with your Deepgram API key
* Hopefully it works. I dunno, I was pretty sleep-deprived when I built this...

Licensed Creative Commons 4.0 Attribution. Feel free to try and use this to make money.
