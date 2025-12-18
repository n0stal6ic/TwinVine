**TwinVine**


![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederA.png)


TwinVine is the home of TWO packages  [Vinefeeder](https://github.com/vinefeeder/TwinVine/blob/main/packages/vinefeeder/src/vinefeeder/README.md)
and [Envied](https://github.com/vinefeeder/TwinVine/blob/main/packages/envied/README.md)

TwinVine is the easy way to handle your download tasks. 
* When you have an exact program url - just use envied as a command line call.
* When you only have the program name - just start with a search in vinefeeder.
* When you dont know what you want; use the browse function.
* Browse media categories like Film, Drama or Sport, for a selected service.
* Batch Mode: select multiple downloads from various services and download all together.
 


**usage**
```
uv run envied dl (options) (service) (program url)

uv run vinefeeder  - to open the interactive GUI for search, browse, etc.

To go to the command line use of 'envied' after using the GUI: close the GUI directly, or ctrl+c in terminal.
```
**Installation** - with binaries already installed

Uv is the package manager and loads both VineFeeder and Envied together.  Envied runs independenly or may be called by Vinefeeder.

If you do not alrealy have uv as a python package install it first, using pip -
```
pip install uv

or

python3 -m pip install uv

or use your system's package manager to install python-uv
```

Then install TwinVine; the following installs the latest version directly from the GitHub repository:

```shell
git clone https://github.com/vinefeeder/TwinVine.git
cd TwinVine
uv clean
uv lock
uv sync
uv run vinefeeder --help or
uv run envied --help
```
**Installation** with a bare machine.

Find and install git:
* Windows:
	download https://github.com/git-for-windows/git/releases/download/v2.52.0.windows.1/Git-2.52.0-64-bit.exe  and run the installer
* Linux: 
	install from your OS' package manager.
Find and install Python:
* Windows:
	search Windows Store for Python and select the latest version available 3.12 or above. 
* Linux:
	Python is probably already installed in basic form.
	
Find and install TwinVine
* Windows or Linux: 
	inside a command window (cmd -w  typed in start for windows or a terminal for Linux) 
	```
	git clone https://github.com/vinefeeder/TwinVine.git
	```
	A folder containing all the TwinVine files will be created. Close the command window but note the location of TwinVine.
	
Install all the required binary files and add to system variable - Path.

* Windows
	- Open Start
	- Type PowerShell
	- Right-click Windows PowerShell → Run as administrator
	- Inside PowerShell change directory to TwinVine and run the following command


	```
	   powershell -ExecutionPolicy Bypass -File .\Install-media-tools.ps1
	
	```
* Linux:
	- Open a terminal window, cd to TwinVine and run
	```
		sudo bash Install-media-tools.sh
	```
* Windows:
Once installation of all the binaries has finished close the PowerShell window with admininstrator privileges and open an ordinary PowerShell window. Change directory to Twinvine
		
	```
	uv clean
	uv lock
	uv sync
	uv run vinefeeder --help or
	uv run envied --help
	```
* Linux:
	- Once the binary install has finished navigate to TwinVine top level folder and run the following commands one by one to build the TwinVine system.
	
	```
	uv clean
	uv lock
	uv sync
	uv run vinefeeder --help or
	uv run envied --help
	```


**Configuration**

Run this line inside the TwinVine folder:
```
cp packages/envied/src/envied/envied-working-example.yaml packages/envied/src/envied/envied.yaml 
```
This ensures you have your own working copy of envied.yaml. It may be edited and will not be over-written during any updates.  
  
That's it; uv run vinefeeder to get started!  

**Linux**

Linux systems are known to screen freeze after envied has finished a download.
The top level vinefeeder config file at  TwinVine/packages/vinefeeder/src/vinefeeder/config.yaml should have   TERMINAL_RESET: True   set.

**Services**

Vinefeeder currently has 10 services for which search, browse and list-select are available  
  
  ALL4  BBC  ITVX  MY5 PLEX RTE STV  TPTV  TVNZ  U 
  
Envied has   

ALL4  AUBC  CBS CWTV DSCP  iP   MAX   MY5   NF   PCOK PLEX RTE  ROKU  SPOT  TPTV  TVNZ  YTBE
ARD   CBC   CTV  DSNP  ITV  MTSP  NBLA  NRK  PLUTO  RTE   STV   TUBI  UKTV  ZDF
These services have web-origins and not all have been tested by me.  
  
**Other README's""
    TwinVine/packages/vinefeeder/src/vinefeeder/README.md  
    for details for confuring Envied download options on a service by service basis.
    TwinVine/packages/envied/README.md  links to wiki (unshackle - envied's parent)

# Newbies
If you are totally new to downloading there are software items that all downloaders call upon to carry out their functions. TwinVine is no different.

You need to have some binaries installed and on your system's Path.
For your security find them from source.

They need to be on your system's Path, 
For linux installng to  /home/user/.local/bin/   is ideal
For Windows it is less clear cut.
If you know how to create a folder and then add the folder to
the Windows systems Environment Path, then do that and place all your binaries
in the new folder.
(I have cheated in the past and used C:\Windows\System32\)

The binary list:
* Python 3.11 or later installed with Linux: or Windows install from the Windows Store
* ffmpeg (https://github.com/FFmpeg/FFmpeg) https://www.videohelp.com/software/ffmpeg  or Linux distro
* N-m3u8DL-RE (https://github.com/nilaoda/N_m3u8DL-RE/releases)
* mp4decrypt (https://github.com/axiomatic-systems/Bento4)
* MKVMerge from MKVToolNix  https://mkvtoolnix.download/downloads.html  https://www.videohelp.com/software/MKVToolNix
* Shaka-packager  https://github.com/shaka-project/shaka-packager/releases  rename the binary to shaka-packager


Images
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederA1.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder1.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder2.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder4.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder5.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder6.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder7.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder8.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder9.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder10.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder11.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederB.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/hellyes.png)
    



