//
// runcached.go
// Copyright (C) 2024 Pavel Vitis <pavelvitis@gmail.com>
//
// Distributed under terms of the MIT license.
//

package main

import (
	"bufio"
	"bytes"
	"crypto/md5"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/ioutil"
	"log"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"
)

const (
	defaultCacheTimeoutSec = 20.0
	maxWaitPrevSec         = 5
	minRandSec             = 0
	maxRandSec             = 0
)

var verbose bool
var debug bool

type CommandCache struct {
	Command      []string
	CacheTimeout float64
	CacheDir     string
	CommandHash  string
	OutputCache  string
	ExitFile     string
	CmdFile      string
}

func getCacheDir() string {
	tmpDir := os.TempDir()
	return tmpDir
}

func GenerateCommandHash(command []string) string {
	h := md5.New()
	h.Write([]byte(strings.Join(command, " ")))
	return hex.EncodeToString(h.Sum(nil))
}

// NewCommandCache creates a new CommandCache instance with the given command and cache timeout.
func NewCommandCache(command []string, cacheTimeout float64) *CommandCache {
	cacheDir := getCacheDir()
	commandHash := GenerateCommandHash(command)
	return &CommandCache{
		Command:      command,
		CacheTimeout: cacheTimeout,
		CacheDir:     cacheDir,
		CommandHash:  commandHash,
		OutputCache:  filepath.Join(cacheDir, fmt.Sprintf("%s.data", commandHash)),
		ExitFile:     filepath.Join(cacheDir, fmt.Sprintf("%s.exit", commandHash)),
		CmdFile:      filepath.Join(cacheDir, fmt.Sprintf("%s.cmd", commandHash)),
	}
}

// IsValid checks if the cached output is still valid based on the cache timeout.
func (cc *CommandCache) IsValid() bool {
    info, err := os.Stat(cc.OutputCache)
    if err != nil {
        return false
    }
	return time.Since(info.ModTime()).Seconds() <= cc.CacheTimeout
}

func (cc *CommandCache) Invalidate() {
	os.Remove(cc.OutputCache)
}

func (cc *CommandCache) CacheResult(output io.Reader) int {
	f, err := os.Create(cc.OutputCache)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to create cache file: %v\n", err)
		return 1
	}
	defer f.Close()

	scanner := bufio.NewScanner(output)
	var returnCode int
	for scanner.Scan() {
		line := scanner.Text()
		_, err := f.WriteString(line + "\n")
		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to write to cache file: %v\n", err)
			returnCode = 1
			break
		}
		fmt.Println(line)
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "Error reading command output: %v\n", err)
		returnCode = 1
	}

	exitFile, err := os.Create(cc.ExitFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to create exit file: %v\n", err)
		return 1
	}
	defer exitFile.Close()

	_, err = exitFile.WriteString(strconv.Itoa(returnCode))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to write exit code to file: %v\n", err)
		return 1
	}

	return returnCode
}

func CustomParse(input string) ([]string, error) {
	var result []string
	var token strings.Builder
	var inQuotes bool
	var quoteChar rune

	for _, r := range input {
		switch {
		case inQuotes:
			if r == quoteChar { // End of quoted segment
				token.WriteRune(r)
				inQuotes = false
			} else {
				token.WriteRune(r)
			}
		case r == '"' || r == '\'':
			inQuotes = true
			quoteChar = r
			token.WriteRune(r) // Keep the opening quote
		case unicode.IsSpace(r):
			if token.Len() > 0 {
				result = append(result, token.String())
				token.Reset()
			}
		default:
			token.WriteRune(r)
		}
	}

	// Add the last token if it exists
	if token.Len() > 0 {
		result = append(result, token.String())
	}

	if inQuotes {
		return nil, fmt.Errorf("unclosed quote: %q", quoteChar)
	}

	return result, nil
}

func ConstructBashCommand(args []string) string {
	// Use shellwords to properly escape and join arguments
	escapedArgs, err := CustomParse(strings.Join(args, ` `))
	if err != nil {
		log.Fatal("Error parsing arguments:", err)
	}

	// Construct the final bash command with all arguments
// 	return "\"" + strings.Replace(strings.Join(escapedArgs, " "), "\"", "\\\"", -1) + "\""
	argsStr := strings.Join(escapedArgs, ` `)
//	argsStr = strings.Replace(argsStr, "\\", "\\\\", -1)
	argsStr = strings.Replace(argsStr, "\"", "\\\"", -1)
	return argsStr
}

func ExecuteCommand(command []string, cache *CommandCache) int {
    command = []string{"bash", "-i", "-c", ConstructBashCommand(command)}
//     log.Fatal(command)
	cmd := exec.Command(command[0], command[1:]...)
	output := &bytes.Buffer{}
	//cmd.Stdout = output
	cmd.Stderr = os.Stderr

	// Create a pipe to capture the output of the command
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		fmt.Println("Error creating pipe:", err)
		return 1
	}

    // Open the cache file for writing
    cacheFile, err := os.Create(cache.OutputCache)
    if err != nil {
        fmt.Fprintf(os.Stderr, "Failed to create cache file: %v\n", err)
        return 1
    }
    defer cacheFile.Close()

    // Combine os.Stdout and cacheFile using io.MultiWriter
    multiWriter := io.MultiWriter(os.Stdout, cacheFile)

	err = cmd.Start()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to start command: %v\n", err)
		return 1
	}

	// Create a wait group to synchronize the Goroutine
	var wg sync.WaitGroup
	wg.Add(1)

    // Use TeeReader to copy the output to both stdout and the buffer
	teeReader := io.TeeReader(stdoutPipe, output)

	// Copy data from the teeReader to os.Stdout on the fly
	go func() {
		defer wg.Done() // Decrement the counter when the Goroutine completes

        _, err := io.Copy(multiWriter, teeReader)
        if err != nil {
            fmt.Println("Error copying to writers:", err)
        }
	}()

	err = cmd.Wait()
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
            // Wait for the Goroutine to finish copying the data
            wg.Wait()

            exitFile, err := os.Create(cache.ExitFile)
            if err != nil {
                fmt.Fprintf(os.Stderr, "Failed to create exit file: %v\n", err)
                return 1
            }
            defer exitFile.Close()

            _, err = exitFile.WriteString(strconv.Itoa(exitErr.ExitCode()))
            if err != nil {
                fmt.Fprintf(os.Stderr, "Failed to write exit code to file: %v\n", err)
                return 1
            }

			return exitErr.ExitCode()
		}
		return 1
	}

	// Wait for the Goroutine to finish copying the data
    wg.Wait()

	return 0
}

func getPidFromFile(fileName string) int {
    data, err := ioutil.ReadFile(fileName)
    if err != nil {
        return -1
    }
    pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
    if err != nil {
        return -1
    }
    return pid
}

func isProcessRunning(pid int) bool {
	// Find the process
	process, err := os.FindProcess(pid)
	if err != nil {
		// Process does not exist
		return false
	}

	// Try to send signal 0 to check if the process is running
	err = process.Signal(syscall.Signal(0))
	if err != nil {
		// If the error is syscall.ESRCH, the process is not running ("no such process")
		if errors.Is(err, syscall.ESRCH) {
			return false
		}
		// Otherwise, the process might not be accessible but is still running
		return true
	}

	return true
}

func waitForPreviousCommand(pidFile string, waitTimeSec int) bool {
	for i := 0; i < waitTimeSec; i++ {
		if _, err := os.Stat(pidFile); os.IsNotExist(err) {
			return true
		}
		pid := getPidFromFile(pidFile)
		// Can't read pid file
		if pid == -1 {
			os.Remove(pidFile)
			return false
		}
		// Stale pid file
		if !isProcessRunning(pid) {
			os.Remove(pidFile)
			return false
		}
		time.Sleep(1 * time.Second)
	}
	return false
}

func CreatePidFile(command []string) string {
	cacheDir := getCacheDir()
	commandHash := GenerateCommandHash(command)
	pidFile := filepath.Join(cacheDir, fmt.Sprintf("%s.pid", commandHash))

	if maxRandSec-minRandSec > 0 {
		time.Sleep(time.Duration(rand.Intn(maxRandSec-minRandSec)+minRandSec) * time.Second)
	}

	if !waitForPreviousCommand(pidFile, maxWaitPrevSec) {
		return ""
	}

	f, err := os.Create(pidFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to create PID file: %v\n", err)
		return ""
	}
	defer f.Close()

	_, err = f.WriteString(strconv.Itoa(os.Getpid()))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to write PID to file: %v\n", err)
		return ""
	}

	return pidFile
}

func sendTextToStdout(textFile string) bool {
	f, err := os.Open(textFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to open file: %v\n", err)
		return false
	}
	defer f.Close()

	_, err = io.Copy(os.Stdout, f)
	if err != nil && err != io.EOF {
		fmt.Fprintf(os.Stderr, "Error copying file to stdout: %v\n", err)
		return false
	}

	return true
}

func readFileOrDefault(path string, defaultValue string) string {
	data, err := ioutil.ReadFile(path)
	if err != nil {
		return defaultValue
	}
	return string(data)
}

func RunCached(command []string, cacheTimeout float64, cacheOnError bool, cacheOnAbort bool) int {
	pidFile := CreatePidFile(command)
	if pidFile == "" {
		fmt.Fprintln(os.Stderr, "Timeout waiting for previous command to finish")
		return 2
	}
	defer os.Remove(pidFile)

	cache := NewCommandCache(command, cacheTimeout)
	if cache.IsValid() {
		if verbose {
			fmt.Println("Returning cached result")
		}
		sendTextToStdout(cache.OutputCache)
		returnCode, _ := strconv.Atoi(string(readFileOrDefault(cache.ExitFile, "0")))
		return returnCode
	} else {
		if verbose {
			fmt.Println("Executing command")
		}
		returnCode := ExecuteCommand(command, cache)
//		cache.CacheResult(output)

		if returnCode != 0 && !cacheOnError {
			cache.Invalidate()
		}
		return returnCode
	}
}

func printUsage() {
    fmt.Fprintln(os.Stderr, "Usage: runcached [-h] [-c CACHE_TIMEOUT] [-e] [-a] [-v] command ...")
}

func printHelp() {
    text := `Run command and cache its output. Return cached output if cache not expired.

positional arguments:
  command ...           Command with arguments

options:
  -h, --help            show this help message and exit
  -c CACHE_TIMEOUT, --cache-timeout CACHE_TIMEOUT
                        Cache timeout in seconds (float), default is 20s
  -e, --cache-on-error  Cache the command result also if it returns nonzero error code
  -a, --cache-on-abort  Cache the command result also on ^C keyboard interrupt
  -d, --debug           Debugging cache information
  -v, --verbose         Print diagnostic information
 `
    fmt.Fprint(os.Stderr, text)
}

// Function to manually escape quotes in arguments
func PreprocessArguments(args []string) []string {
	var processedArgs []string
	for _, arg := range args {
		// Escape backslashes if any
		arg = strings.ReplaceAll(arg, `\`, `\\`)
		// Preserve double-quotes by escaping them
		arg = strings.ReplaceAll(arg, `"`, `\"`)
		// Preserve quotes by escaping them
		if len(arg) >= 2 && arg[0] == '\'' && arg[len(arg)-1] == '\'' {
			arg = "\\'" + arg[1 : len(arg)-1] + "\\'"
		}
		processedArgs = append(processedArgs, arg)
	}
	return processedArgs
}

func main() {
// 	var cacheTimeout float64
	var cacheOnError, cacheOnAbort bool

    helpFlag := flag.Bool("help", false, "Show help")
	flag.BoolVar(helpFlag, "h", false, "Show help")
	cacheTimeoutFlag := flag.Float64("c", defaultCacheTimeoutSec, "Cache timeout in seconds")
	flag.Float64Var(cacheTimeoutFlag, "cache-timeout", defaultCacheTimeoutSec, "Cache timeout in seconds")
	flag.BoolVar(&cacheOnError, "e", false, "Cache command result even on nonzero error code")
	flag.BoolVar(&cacheOnAbort, "a", false, "Cache command result even on keyboard interrupt")
	flag.BoolVar(&debug, "d", false, "Enable cache debugging output")
	flag.BoolVar(&verbose, "v", false, "Enable verbose mode")
	args := os.Args[1 : len(os.Args)]
	flag.CommandLine.Parse(PreprocessArguments(args))

    if *helpFlag {
	    printUsage()
	    printHelp()
		os.Exit(1)
    }

	if flag.NArg() < 1 {
	    printUsage()
		os.Exit(1)
	}


//     log.Fatal(os.Args)
//     log.Fatal(flag.Args())
	command := flag.Args()

    os.Exit(RunCached(command, *cacheTimeoutFlag, cacheOnError, cacheOnAbort))
}
