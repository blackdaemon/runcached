package main

import (
	"os"
	"strings"
	"testing"
	"time"

	"runcached"
)

// Test for generating command hash
func TestGenerateCommandHash(t *testing.T) {
	command := []string{"echo", "hello"}
	hash := main.GenerateCommandHash(command)
	if hash == "" {
		t.Error("Command hash should not be empty")
	}
}

// Test for creating a CommandCache instance
func TestNewCommandCache(t *testing.T) {
	command := []string{"ls", "-l"}
	cacheTimeout := 10.0
	cache := main.NewCommandCache(command, cacheTimeout)
	if cache.CacheDir == "" || cache.CommandHash == "" {
		t.Error("Cache directory or command hash should not be empty")
	}
}

// Test for cache validation
func TestCacheValidation(t *testing.T) {
	command := []string{"ls", "-l"}
	cache := main.NewCommandCache(command, 1.0)

	// Create a dummy cache file
	os.WriteFile(cache.OutputCache, []byte("cached data"), 0644)
	defer os.Remove(cache.OutputCache)

	if !cache.IsValid() {
		t.Error("Cache should be valid")
	}

	time.Sleep(2 * time.Second)
	if cache.IsValid() {
		t.Error("Cache should be invalid after timeout")
	}
}

// Test for argument preprocessing
func TestPreprocessArguments(t *testing.T) {
	args := []string{"arg1", "'arg with quotes'", `"double quotes"`, `back\slash`}
	expected := []string{"arg1", "\\'arg with quotes\\'", "\\\"double quotes\\\"", "back\\\\slash"}
	processedArgs := main.PreprocessArguments(args)

	for i, arg := range processedArgs {
		if arg != expected[i] {
			t.Errorf("Expected %s but got %s", expected[i], arg)
		}
	}
}

// Test for constructing bash command
func TestConstructBashCommand(t *testing.T) {
	args := []string{"ls", "-l", `"file with\\ spaces"`}
	expected := `ls -l \"file with\\\\ spaces\"`
	command := main.ConstructBashCommand(args)

	if command != expected {
		t.Errorf("Expected %s but got %s", expected, command)
	}
}

// Test for command execution
func TestExecuteCommand(t *testing.T) {
	command := []string{"echo", "hello"}

	cache := main.NewCommandCache(command, 1)
	code := main.ExecuteCommand(command, cache)

	if code != 0 {
		t.Errorf("Expected exit code 0 but got %d", code)
	}
}

// Test for caching results
func TestCacheResult(t *testing.T) {
	command := []string{"echo", "hello"}

	cache := main.NewCommandCache(command, 10.0)
	defer os.Remove(cache.OutputCache)

	main.ExecuteCommand(command, cache)

	data, err := os.ReadFile(cache.OutputCache)
	if err != nil {
		t.Errorf("Failed to read cache file: %v", err)
	}

	if !strings.Contains(string(data), "hello") {
		t.Error("Cache does not contain expected data")
	}
}

// Test for PID file creation
func TestCreatePidFile(t *testing.T) {
	command := []string{"ls", "-l"}
	pidFile := main.CreatePidFile(command)
	defer os.Remove(pidFile)

	if _, err := os.Stat(pidFile); os.IsNotExist(err) {
		t.Error("PID file was not created")
	}
}

// Test for command caching functionality
func TestRunCached(t *testing.T) {
	command := []string{"echo", "cached"}
	cacheTimeout := 2.0
	returnCode := main.RunCached(command, cacheTimeout, false, false)

	if returnCode != 0 {
		t.Errorf("Expected return code 0 but got %d", returnCode)
	}

	// Ensure cache is used
	time.Sleep(1 * time.Second)
	returnCode = main.RunCached(command, cacheTimeout, false, false)
	if returnCode != 0 {
		t.Errorf("Expected return code 0 from cache but got %d", returnCode)
	}
}
