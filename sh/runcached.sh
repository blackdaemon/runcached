#!/bin/bash

# Run an expensive command as frequently as you want through this. 
# Command output will be kept for $cacheperiod and replayed for subsequent calls
# Possible usage: query multiple EMC parameters with zabbix in random order, 
#                by running the EMC interfacing command just once, transparrently to zabbix.
# sivann 2012
# Thu Jun 14 13:11:28 EEST 2012
####################################################33
cacheperiod=60 #seconds
savedir="/tmp"
####################################################33

for arg in "$@"
do
    cmd=( "${cmd[@]}" "$arg" )
    let "i+=1"
done

cmdmd5=`echo -n "${cmd[@]}" |md5sum|awk '{print $1}'`

# random sleep to avoid racing condition of creating the pid on the same time
sleep .$[ ( $RANDOM % 10 ) + 1 ]s

# don't run the same command in parallel, wait for it to finish the 1st time
# don't bother with locks, even advisory are buggy in several IOT devices with ancient shells and kernels
count=15
while [ -f /tmp/${cmdmd5}-runcached.pid ]; do
    sleep 2
    count=`expr $count -1`
    if [ $count -eq 0 ]; then
        echo "timeout waiting for runcached.pid to be removed"
        exit -1
    fi
done

echo $$ >/tmp/${cmdmd5}-runcached.pid


cachedir="/tmp"
cmddatafile="${cachedir}/${cmdmd5}.data"
cmdexitcode="${cachedir}/${cmdmd5}.exitcode"
cmdfile="${cachedir}/${cmdmd5}.cmd"

##########

function runit {
    ${cmd[@]} 2>&1 | tee $cmddatafile 1>/dev/null 2>&1
    exitcode=${PIPESTATUS[0]}
    echo $exitcode > $cmdexitcode
    echo  "${cmd[@]}" > $cmdfile
}


if [ ! -f "$cmddatafile"  ] ; then  runit ; fi

if [[ $(uname -s) = "Darwin" ]] ; then
    lastrun=`stat -f %m $cmddatafile`
else
    lastrun=`stat -c %Y $cmddatafile`
fi
currtime=`date +%s`
diffsec=$(( (currtime - lastrun) ))

if [ "$diffsec"  -ge "$cacheperiod" ] ; then
    runit 
fi

cat $cmddatafile

/bin/rm /tmp/${cmdmd5}-runcached.pid

exit `cat $cmdexitcode`
