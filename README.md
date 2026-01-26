# ambistream
An improved implementation of weather/time dependent radio

Ambistream is a successor to [AutoBreezeBeats](https://github.com/SK1Y101/AutoBreezeBeats), offering a more polished and improved listening experience.

Local hosted web app, YouTube streamed and cached audio, served to a URL endpoint with a second configuration and queueng window.

Ambistream requires an [OpenWeatherMap](https://openweathermap.org/api) API key to curate music from weather.


## config

There exists a `config.yaml.sample` at the top level of this repository. Populate with your api key and location and save as `config.yaml` for the weather/music player to

## running

Ensure you have nox, ffmpeg, and python >=3.12 on your system.

running the script will then be as simple as `nox -s run` to launch all sub-systems,

Nox will manage python dependencies for you, first execution may take a while as it downloads them.

### individually

it is possible to launch each system individually:
- `nox -s player` -> is in control of playing music
- `nox -s ui` -> allows modifying song metadata
- `nox -s orchestrator` -> automatically queue songs from the database
