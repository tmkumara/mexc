"""Standalone 4H RSI and endpoint Nadaraya-Watson Envelope research scanner.

Envelope calculation adapted from Nadaraya-Watson Envelope [LuxAlgo].
© LuxAlgo. Licensed CC BY-NC-SA 4.0:
https://creativecommons.org/licenses/by-nc-sa/4.0/
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

BAR = pd.Timedelta(hours=4)
KERNEL_BARS = 500
ERROR_BARS = 499
BANDWIDTH = 8.
MULTIPLIER = 3.


def indicators(closes):
    result = pd.DataFrame(index=closes.index)
    delta=closes.diff().iloc[1:]
    if len(delta)>=14:
        averages=[]
        for values in (delta.clip(lower=0),(-delta).clip(lower=0)):
            seeded=pd.concat([values.iloc[13:14]*0+values.iloc[:14].mean(),values.iloc[14:]])
            averages.append(seeded.ewm(alpha=1/14,adjust=False).mean())
        up,down=averages
        result["rsi14"]=(100-100/(1+up/down)).mask(down.eq(0),100)
    else:
        result["rsi14"]=np.nan
    result["center"]=np.nan
    if len(closes)>=KERNEL_BARS:
        lags=np.arange(KERNEL_BARS)
        weights=np.exp(-.5*(lags/BANDWIDTH)**2)
        center=np.convolve(closes.to_numpy(),weights/weights.sum(),mode="valid")
        result.loc[closes.index[KERNEL_BARS-1:],"center"]=center
    width=(closes-result.center).abs().rolling(ERROR_BARS).mean()*MULTIPLIER
    result["lower"]=result.center-width
    result["upper"]=result.center+width
    return result


def signal_frame(closes, *, duration=BAR):
    output=indicators(closes)
    output["close"]=closes
    output["signal"]=""
    output["excursion_rsi"]=np.nan
    position=0
    extreme=None
    for row in output.itertuples():
        if not np.isfinite(row.lower) or not np.isfinite(row.rsi14):
            continue
        if row.close<row.lower:
            position,extreme=1,min(extreme,row.rsi14) if position==1 else row.rsi14
        elif row.close>row.upper:
            position,extreme=-1,max(extreme,row.rsi14) if position==-1 else row.rsi14
        else:
            if position==1 and extreme<=30:
                output.loc[row.Index,["signal","excursion_rsi"]]=["LONG",extreme]
            elif position==-1 and extreme>=70:
                output.loc[row.Index,["signal","excursion_rsi"]]=["SHORT",extreme]
            position,extreme=0,None
    output.index+=duration
    output.index.name="closed_at"
    return output


def load_closed(source, interval, as_of):
    data=pd.read_csv(source,usecols=["timestamp","close"])
    data.index=pd.to_datetime(data.timestamp,utc=True)
    step=pd.Timedelta(minutes=interval)
    data=data.loc[data.index+step<=as_of]
    if not data.index.equals(data.index.floor(step)) or not data.index.to_series().diff().dropna().eq(step).all():
        raise ValueError("Expected contiguous UTC-aligned candles")
    if not np.isfinite(data.close).all() or not data.close.gt(0).all():
        raise ValueError("Expected finite positive prices")
    if interval==240:
        return data.close
    grouped=data.close.resample("4h")
    return grouped.last().loc[grouped.size().eq(16)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv",type=Path,required=True)
    parser.add_argument("--as-of",required=True)
    parser.add_argument("--minutes",type=int,choices=(15,240),default=15)
    parser.add_argument("--out",type=Path)
    args=parser.parse_args()
    output=signal_frame(load_closed(args.csv,args.minutes,pd.to_datetime(args.as_of,utc=True)))
    eligible=output.dropna(subset=["lower"])
    if eligible.empty:
        parser.error(f"Envelope requires {KERNEL_BARS+ERROR_BARS-1} closed 4H candles")
    if args.out:
        output.loc[output.signal.ne("")].to_csv(args.out)
    print(eligible.tail(1).to_string())
    print(f"Envelope ready from {eligible.index[0]}; {eligible.signal.ne('').sum()} historical alerts")


if __name__=="__main__":
    main()
