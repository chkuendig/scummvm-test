echo "BASEURL: $DATA_BASEURL"
echo "SSHPATH: $SSH_PATH"
# keep demos in sync and create games.json
python3 scripts/sync-games.py --max-transfers 10

# update baseURL
jq '.games +=  {"baseUrl":"$DATA_BASEURL"}' scummvm/build-emscripten/data/index.json > scummvm/build-emscripten/data/index.json.tmp && mv scummvm/build-emscripten/data/index.json.tmp scummvm/build-emscripten/data/index.json

# copy everything
cp  assets/games.html scummvm/build-emscripten/
cp  games.json scummvm/build-emscripten/