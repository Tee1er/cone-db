import pandas as pd
from pathlib import Path
import json
from dateutil import parser
import re
import sys

from utils import calculate_HRR, calculate_MFR, calculate_k, colorize

args = sys.argv[1:]
if len(args) > 2:
    print("""Too many arguments
          Usage: python parse-MIDAS.py <input_dir> <output_dir>
          Leave empty to use defaults.""")
    sys.exit()

# these assume the script is being run from the root of the repo
INPUT_DIR = Path(r"\\nfrl.el.nist.gov\NFRL_DATA\FRD224ConeCalorimeter")
OUTPUT_DIR = Path(r"./data/auto-processed/MIDAS")

if len(args) == 2:
    INPUT_DIR = Path(args[0])
    OUTPUT_DIR = Path(args[1])

files_parsed = 0
files_parsed_successfully = 0

negative_pe_tests = 0
bad_i_tests = 0

#region parse_dir
def parse_dir(input_dir):
    # glob all (scaled) csv files in the input directory
    paths = Path(input_dir).glob("**/*scaled.csv")

    # create the output directory if it doesn't exist
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    for path in paths:
        global files_parsed_successfully
        global files_parsed
        global negative_pe_tests
        global bad_i_tests

        # Every 20 files, print out file success rate:
        if files_parsed % 20 == 0 and files_parsed != 0:
            print(colorize(f"Files parsed successfully: {files_parsed_successfully}/{files_parsed} ({(files_parsed_successfully/files_parsed) * 100}%)", "blue"))

        try:
            files_parsed += 1
            parse_file(path)
        except Exception as e:
            print(colorize(f" - Error parsing {path}: {e}", "red"))
            print()
            continue
        
        print(colorize("Parsed successfully\n", "green"))

        files_parsed_successfully += 1
    
    # print out stats about the files parsed
    print(colorize(f"Files parsed successfully: {files_parsed_successfully}/{files_parsed} ({(files_parsed_successfully/files_parsed) * 100}%)", "purple"))
    print(colorize(f"Tests with bad light intensity data (no Ksmoke): {bad_i_tests}", "purple"))
    print(colorize(f"Skipped due to negative delta_P: {negative_pe_tests}", "purple"))

#region parse_file
def parse_file(file_path):
    print(f"Parsing {file_path}")

    # read in scaled CSV file as pd dataframe
    df = pd.read_csv(file_path, encoding="cp1252")

    # drop rows with all NaN/null/etc. values
    df = df.dropna(how="all")

    metadata = parse_metadata(file_path)

    data = parse_data(df, metadata)

    # If there's less than 20 data points, just skip the file
    if len(data) < 20:
        print(colorize(f"Skipping {file_path} because it has less than 20 seconds of data", "yellow"))
        return

    # The output folder has directory structure based on the test year
    # therefore, we need to get the year from the date in the metadata
    test_year = parser.parse(metadata["date"]).year

    # Create the year folder if it doesn't exist yet
    Path(OUTPUT_DIR / str(test_year)).mkdir(parents=True, exist_ok=True)

    # Determine output paths
    metadata_output_path = Path(OUTPUT_DIR) / str(test_year) / f"{Path(file_path).stem.replace("-scaled", "")}.json"
    data_output_path = Path(OUTPUT_DIR) / str(test_year) / f"{Path(file_path).stem.replace("-scaled", "")}.csv"

    # write metadata to json file
    with open(metadata_output_path, "w") as f:
        json.dump(metadata, f, indent=4)

    # write data to csv file as well
    data.to_csv(data_output_path, index=False)

#region parse_metadata
def parse_metadata(file_path):
    # get the -Output .xls file for the metadata
    file_path = str(file_path).replace("-scaled.csv", "-Output.xls")

    if Path(file_path).is_file() == False:
        raise Exception(f"Missing -Output metadata file")

    # read in different sheets of the -Output .xls file
    # three tabs in MIDAS output files: Parameters, Info, and User Events
    # User Events is parsed later b/c it is not always present in all tests
    params = pd.read_excel(file_path, "Parameters")
    # TODO: also consider "ReportInfo" sheet
    info = pd.read_excel(file_path, "Info", header=None)

    # convert params to a dictionary
    params = params.to_dict(orient="list")
    params = {k: v[0] if len(v) > 0 else None for k, v in params.items()}

    # helper functions to get values from the params & info dictionaries
    def get_number(key, dict):
        try:
            return float(dict[key])
        except:
            return None

    def get_bool(key, params):
        value = params.get(key, None)
        if value is None:
            return None
        value = value.lower()
        if value == "no" or value == "false":
            return False
        elif value == "yes" or value == "true":
            return True
        return None

    metadata = {}

    #region test parameters
    metadata["c_factor"] = get_number("Cf", params)
    e = get_number("Ef", params)
    if e is None:
        e = 13100 #kj/kg
        print(colorize(" - Ef not defined in metadata, defaulting to 13.1", "yellow"))
    elif e == 13.1:
        e = 13100
    elif e == 12.54:
        e = 12540
    e /= 1000
    metadata["e_mj/kg"] = e
    metadata["heat_flux_kW/m2"] = get_number("CONEHEATFLUX", params)
    metadata["grid"] = get_bool("Grid", params)
    metadata["separation_mm"] = get_number("Separation", params)
    metadata["initial_mass_g"] = get_number("ISMass", params)
    metadata["orientation"] = params.get("ORIENTATION")

    area = get_number("As", params)
    if area is None:
        raise Exception("Area not defined in metadata")
    
    # if the area is less than 0.025, it's probably in square meters rather than square cm, so multiply by 100^2 to convert it
    elif area <= 0.025:
        metadata["surface_area_cm2"] = area  * 100**2
    else:
        metadata["surface_area_cm2"] = area

    #region general test info

    # get the transpose of the dataframe (swaps rows & columns)
    info = info.T
    
    new_header = info.iloc[0]
    info = info[1:]
    info.columns = new_header
    # transform into a dictionary
    info = info.to_dict(orient="list")

    # if there are multiple values, take the first one
    info = {k: v[0] if len(v) > 0 else None for k, v in info.items()}
    info = {k.replace(":", ""): v for k, v in info.items()}

    # parse dates
    date = parser.parse(f"{info["Date"]} {info["Time"]}", dayfirst=False)

    # add date to the metadata dictionary as an ISO 8601 string
    metadata["date"] = date.isoformat()
    metadata["operator"] = info.get("Qualified Operator")
    metadata["director"] = info.get("Test Director")
    metadata["comments"] = info.get("Test Series information")
    metadata["specimen_number"] = info.get("Sample ID")

    #region events

    # the .read_excel() function will raise an error if the sheet doesn't exist, so if the events sheet doesn't exist, just return the metadata now
    try:
        events = pd.read_excel(file_path, sheet_name="User Events")
    except Exception as e:
        print(f" - Missing user events: {e}")
        return metadata
    
    def parse_event(row):
        return {
            # shift event back by start time
            "time": row["Time (s)"],
            "event": row["Event Description"],
        }
    
    parsed_events = [parse_event(row) for _, row in events.iterrows()]

    metadata["events"] = parsed_events

    # use event info to set ignition time, burnout, start, etc.
    for event in parsed_events:
        if "Ignition" in event["event"]:
            metadata["time_to_ignition_s"] = event["time"]
        elif "flame out" in event["event"].lower() or "fire out" in event["event"].lower():
            metadata["time_to_flameout_s"] = event["time"]
        elif "Start" in event["event"]:
            metadata["test_start_time_s"] = event["time"]

    return metadata

#region parse_data
def parse_data(df, metadata):

    # get rid of the numbers in the column names using regex
    df.columns = [re.sub(r"\d+:\s*", "", x) for x in df.columns]

    # Convert Te into K
    df["Te (°C)"] += 273.15

    #region final columns

    # rename columns for consistency
    data = df.rename(
        columns={
            "Test Time (s)": "Time (s)",
            "O2 (Vol fr)": "O2 (Vol fr)",
            "CO2 (Vol fr)": "CO2 (Vol fr)",
            "CO (Vol fr)": "CO (Vol fr)",
            "Te (°C)": "Te (K)",
            "Pe (Pa)": "Pe (Pa)",
            "Io (%)": "I_o (%)",
            "I (%)": "I (%)",
            # "RHRA (kW/m2)": "original HRR (kW/m2)",
            "SampMass (g)": "Mass (g)",
        }
    )

    # If the Time (s) column has increments besides 1, raise an error
    if data["Time (s)"].diff().max() > 1:
        raise Exception("Time increments are not 1 second")

    data = process_data(data, metadata)

    # set which columns to include in the final output
    data = data[["Time (s)", "O2 (Vol fr)", "CO2 (Vol fr)", "CO (Vol fr)", "HRR (kW/m2)", "MFR (kg/s)", "k_smoke (1/m)","Mass (g)"]]

    return data

#region process_data
def process_data(data, metadata):

    # test parameters used for calculations
    # second value is the default value if the parameter is not defined in the metadata
    start_time = metadata.get("test_start_time_s", 0)

    o2_delay = metadata.get("o2_delay_time_s", 0)
    co2_delay = metadata.get("co2_delay_time_s", 0)
    co_delay = metadata.get("co_delay_time_s", 0)
    area = metadata.get("surface_area_cm2", 100)

    c_factor = metadata.get("c_factor")
    e = metadata.get("e_mj/kg", 13.1)

    # convert area from cm2 to m2
    area = area / (100**2)

    #region delay, baselines

    # if start-time is not defined, just use the first 30 secs for baseline
    baseline_end = int(start_time if start_time > 0 else 30)

    # calculate baseline values by using the data up to test start time
    X_O2_initial = data["O2 (Vol fr)"].iloc[:baseline_end].mean() # / 100
    X_CO2_initial = data["CO2 (Vol fr)"].iloc[:baseline_end].mean() # / 100
    X_CO_initial = data["CO (Vol fr)"].iloc[:baseline_end].mean() # / 100

    # shift entire dataframe up to start time
    data = data.shift(-start_time)
    data.drop(data.tail(start_time).index, inplace=True)
    data["Time (s)"] -= start_time

    # shift certain columns up to account for O2, CO, CO2 analyzer time delay, and remove the rows at the end
    data["O2 (Vol fr)"] = data["O2 (Vol fr)"].shift(-o2_delay)
    data["CO2 (Vol fr)"] = data["CO2 (Vol fr)"].shift(-co2_delay)
    data["CO (Vol fr)"] = data["CO (Vol fr)"].shift(-co_delay)

    data.drop(data.tail(max(o2_delay, co_delay, co2_delay)).index, inplace=True)

    global negative_pe_tests

    # If delta_P is negative, the data is probably not useful, just throw an error
    if data["Pe (Pa)"].min() < 0:
        negative_pe_tests += 1
        raise Exception("Negative delta_P found")

    # Calculate HRR by row

    #region calc. HRR, MFR, k

    def get_HRR(row):
        X_O2 = row["O2 (Vol fr)"]
        X_CO2 = row["CO2 (Vol fr)"]
        X_CO = row["CO (Vol fr)"]

        delta_P = row["Pe (Pa)"]
        T_e = row["Te (K)"]

        return calculate_HRR(
            X_O2,
            X_CO2,
            X_CO,
            X_O2_initial,
            X_CO2_initial,
            delta_P,
            T_e,
            c_factor,
            e,
            area,
        )

    # calculate HRR for each row
    data["HRR (kW/m2)"] = data.apply(get_HRR, axis=1)


    def get_MFR(row):
        delta_P = row["Pe (Pa)"]
        T_e = row["Te (K)"]

        return calculate_MFR(c_factor, delta_P, T_e)

    # calculate MFR for each row
    data["MFR (kg/s)"] = data.apply(get_MFR, axis=1)

    # Calculate K_smoke by row
    def get_k(row):
        # print(row)
        I_o = row["I_o (%)"]
        I = row["I (%)"]
        # TODO: figure out what path length should be (not in any of the metadata files)
        L = metadata.get("path_length_m", 1)

        return calculate_k(I_o, I, L)
    
    global bad_i_tests

    # check if I_o or I even exists
    if "I_o (%)" not in data.columns or "I (%)" not in data.columns:
        print(colorize(" - I_o or I not found in data, skipping k_smoke calculation", "yellow"))
        bad_i_tests += 1
        data["k_smoke (1/m)"] = None
        return data
    # if I_o or I is negative, the smoke data is probably not useful, just return early
    elif data["I_o (%)"].min() < 0 or data["I (%)"].min() < 0:
        print(colorize(" - I_o or I is negative, skipping k_smoke calculation", "yellow"))
        bad_i_tests += 1
        data["k_smoke (1/m)"] = None
        return data
    
    # calculate k_smoke for each row
    data["k_smoke (1/m)"] = data.apply(get_k, axis=1)

    return data

# if the script is being run directly (not as an imported module), then run the parse_dir function
if __name__ == "__main__":
    parse_dir(INPUT_DIR)
    # parse_file(r"\\nfrl.el.nist.gov\NFRL_DATA\FRD224ConeCalorimeter\DATA\2019\08_Aug\PVC_and_Wood_Fence\8-5-2019-CONELAB-PVC-Fen-1-scaled.csv")

    
