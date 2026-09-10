import sys
import os
import argparse

# Add parent directory to sys.path so we can import preprocessing module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from preprocessing.requests_ad_check import _check_ads_txt

def main():
    parser = argparse.ArgumentParser(description="Test if a website has a valid ads.txt file.")
    parser.add_argument("url", help="The website URL to check (e.g., https://cnn.com)")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout in seconds")
    parser.add_argument("--verify-ssl", action="store_true", default=False, help="Verify SSL certificates")
    
    args = parser.parse_args()
    
    print(f"Testing ads.txt for: {args.url}")
    
    result = _check_ads_txt(args.url, args.timeout, args.verify_ssl)
    
    if result == 1:
        print("Result: 1 - Valid ads.txt found!")
    elif result == -1:
        print("Result: -1 - ads.txt exists but is empty or has no valid entries.")
    else:
        print("Result: 0 - No valid ads.txt found or invalid content type.")

if __name__ == "__main__":
    main()
