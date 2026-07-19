import subprocess


class SDRService:

    def stop(self):
        subprocess.run("pkill rtl_fm; pkill sox", shell=True)

    def play_channel(self, freq, seconds=0.8, squelch=100):
        cmd = (
            f"timeout {seconds} rtl_fm "
            f"-p -2 "
            f"-f {freq}M "
            f"-M fm "
            f"-s 48000 "
            f"-g 40 "
            f"-l {squelch} "
            f"| sox -q -t raw -r 48000 -e signed -b 16 -c 1 - -d"
        )

        subprocess.run(cmd, shell=True)

    def play_weather(self):
        cmd = (
            "rtl_fm -p -2 -f 162.425M -M fm -s 48000 -g 40 -l 0 "
            "| sox -q -t raw -r 48000 -e signed -b 16 -c 1 - -d"
        )
        subprocess.Popen(cmd, shell=True)


sdr = SDRService()
