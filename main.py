from generate_endpoints import (
    generate_endpoint_file
)

from fetch_cases import (
    fetch_test_cases
)

def main():
    endpoint_file = generate_endpoint_file(
        "data/GRCX-web.csv"
    )

    results = fetch_test_cases(
        endpoint_file
    )

    for result in results:
        print("ID:", result["ID"])
        print("Status:", result["status_code"])
        print("Response:", result["response"])
        print("-" * 50)


if __name__ == "__main__":
    main()