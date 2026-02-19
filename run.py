import time

def run_forever():
    print("Scanner running...")
    while True:
        print("Still alive...")
        time.sleep(10)

if __name__ == "__main__":
    run_forever()