import csv
import os

INPUT_FILE = "wallets_export.csv"   # Your original file
OUTPUT_FILE = "addresses_only.csv"  # The new file with only addresses

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: Could not find '{INPUT_FILE}'.")
        return

    print(f"Reading '{INPUT_FILE}' and extracting only addresses...")

    with open(INPUT_FILE, mode='r', newline='') as infile, \
         open(OUTPUT_FILE, mode='w', newline='') as outfile:
        
        reader = csv.DictReader(infile)
        
        # Ensure the Address column exists
        if 'Address' not in reader.fieldnames:
            print("ERROR: Source CSV does not have an 'Address' header.")
            return

        # Set up the writer with ONLY the Address column
        writer = csv.DictWriter(outfile, fieldnames=["Address"])
        writer.writeheader()
        
        count = 0
        for row in reader:
            # Write only the Address to the new file
            writer.writerow({"Address": row["Address"]})
            count += 1

    print(f"Done! Saved {count} addresses to '{OUTPUT_FILE}'.")
    print(f"You can now safely delete '{INPUT_FILE}'.")

if __name__ == "__main__":
    main()