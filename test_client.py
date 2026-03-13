import asyncio
import websockets
import json
import wave

# Configuración basada en tu .env y docker-compose
WS_URI = "ws://localhost:8005/ws"
AUTH_TOKEN = "test-token"  # El StubAuthProvider acepta cualquier cosa
SAMPLE_RATE = 24000  # Definido en su configuración
OUTPUT_FILE = "test_output.wav"

async def test_speaker():
    try:
        async with websockets.connect(WS_URI) as websocket:
            print(f"Conectado a {WS_URI}")

            # 1. Enviar mensaje de autenticación
            auth_msg = {
                "type": "auth",
                "token": AUTH_TOKEN
            }
            await websocket.send(json.dumps(auth_msg))
            
            # Esperar respuesta de auth
            response = await websocket.recv()
            resp_data = json.loads(response)
            if resp_data.get("type") != "auth_ok":
                print(f"Error de autenticación: {resp_data}")
                return
            print("Autenticación exitosa.")

            # 2. Enviar tokens de texto (simulando un flujo en tiempo real)
            tokens = ["Hola,", " esto es", " una prueba", " de streaming", " con Kokoro."]
            for token in tokens:
                print(f"Enviando token: {token}")
                msg = {
                    "type": "token",
                    "text": token
                }
                await websocket.send(json.dumps(msg))
                await asyncio.sleep(0.5)  # Simular retraso entre tokens

            # 3. Enviar mensaje de finalización
            await websocket.send(json.dumps({"type": "end"}))
            print("Mensaje 'end' enviado. Recibiendo audio...")

            # 4. Procesar mensajes del servidor y guardar audio
            audio_data = bytearray()
            
            while True:
                try:
                    message = await websocket.recv()
                    
                    # El servidor envía tanto texto (JSON) como binario (Audio)
                    if isinstance(message, str):
                        data = json.loads(message)
                        msg_type = data.get("type")
                        
                        if msg_type == "audio_start":
                            print(f"Empezando chunk de audio {data.get('chunk_id')}...")
                        elif msg_type == "audio_end":
                            print(f"Finalizado chunk {data.get('chunk_id')}.")
                        elif msg_type == "done":
                            print("Generación completada por el servidor.")
                            break
                        elif msg_type == "error":
                            print(f"Error del servidor: {data.get('message')}")
                            break
                    else:
                        # Recibiendo frames binarios de PCM16
                        audio_data.extend(message)
                
                except websockets.exceptions.ConnectionClosed:
                    break

            # 5. Guardar el resultado en un archivo WAV
            if audio_data:
                with wave.open(OUTPUT_FILE, 'wb') as wav_file:
                    wav_file.setnchannels(1)  # Mono
                    wav_file.setsampwidth(2)  # PCM16 = 2 bytes
                    wav_file.setframerate(SAMPLE_RATE)
                    wav_file.writeframes(audio_data)
                print(f"Audio guardado exitosamente en: {OUTPUT_FILE}")
            else:
                print("No se recibió audio.")

    except Exception as e:
        print(f"Error en la conexión: {e}")

if __name__ == "__main__":
    asyncio.run(test_speaker())