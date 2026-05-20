import pandas as pd
import argparse

def process_spreadsheet(input_file, output_file, column_to_check, cols_to_drop, row_limit=0):
    try:
        # 1. Load the spreadsheet
        df = pd.read_excel(input_file)

        if row_limit and row_limit > 0:
            df = df.head(row_limit)
        
        # 2. Drop the specified columns
        # axis=1 signifies columns
        df = df.drop(columns=cols_to_drop, errors='ignore')
        
        # 3. Filter out rows where the target column is empty
        # First, ensure data is clean (remove whitespace/convert empty cells to NA)
        df[column_to_check] = df[column_to_check].replace(r'^\s*$', pd.NA, regex=True)
        
        # Now drop rows where the target column is NaN
        cleaned_df = df.dropna(subset=[column_to_check])
        
        # 4. Save the result
        cleaned_df.to_excel(output_file, index=False)
        print(f"Success! Processed data saved to {output_file}")
        
    except Exception as e:
        print(f"An error occurred: {e}")


def build_parser():
    parser = argparse.ArgumentParser(description="Clean the final workbook after scraping.")
    parser.add_argument("--max-rows", type=int, default=0, help="Limit how many rows are processed for a test run.")
    return parser

# --- Configuration ---
# Replace with your actual file names and column names
input_filename = 'north_india_deep_contacts.xlsx'
output_filename = 'cleaned_data.xlsx'
target_column = 'placement_emails'  # Row will be deleted if this is empty
columns_to_remove = ['placement_page_url', 'faculty_page_url'] # Names of columns to delete

if __name__ == '__main__':
    args = build_parser().parse_args()
    process_spreadsheet(input_filename, output_filename, target_column, columns_to_remove, row_limit=args.max_rows)